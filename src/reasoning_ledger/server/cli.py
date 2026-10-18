"""Host-local initialization, account recovery, and public server startup."""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
from pathlib import Path

import uvicorn

from reasoning_ledger.deployment.host import InstallError

from . import recovery, rollback
from .api import Bootstrap, Config, create_app
from .backup import DEFAULT_BYTES, backup, create_key, inspect_backup, restore
from .mail import MailConfig
from .maintenance import finish, migrate, observe_activation, serving, verify_prepared
from .store import Store, StoreError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reasoning-server")
    parser.add_argument("--data-dir", type=Path, required=True, help="private installation state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="initialize state and create a private one-time setup credential")
    key = commands.add_parser("backup-key", help="generate a private random backup encryption key without overwriting")
    key.add_argument("--output", type=Path, required=True)
    snapshot = commands.add_parser("backup", help="create and verify an encrypted online SQLite/artifact snapshot")
    snapshot.add_argument("--output", type=Path, required=True)
    snapshot.add_argument("--key-file", type=Path, required=True)
    snapshot.add_argument("--maximum-bytes", type=int, default=DEFAULT_BYTES)
    migration = commands.add_parser(
        "migrate-offline", help="verify a matching encrypted snapshot and prepare a journaled offline migration"
    )
    migration.add_argument("--backup", type=Path, required=True)
    migration.add_argument("--key-file", type=Path, required=True)
    migration.add_argument("--expected-sha256", required=True)
    migration.add_argument("--source-schema", type=int, required=True)
    migration.add_argument("--target-release", required=True)
    migration.add_argument("--operation-id", required=True)
    migration.add_argument("--confirm-source-stopped", action="store_true")
    migration.add_argument("--maximum-bytes", type=int, default=DEFAULT_BYTES)
    completion = commands.add_parser(
        "maintenance-finish", help="retain immutable maintenance history after operator-verified target activation"
    )
    completion.add_argument("--operation-id", required=True)
    completion.add_argument("--target-release", required=True)
    completion.add_argument("--confirm-activation-verified", action="store_true")
    verification = commands.add_parser("maintenance-verify", help="validate exact prepared offline state without starting the server")
    verification.add_argument("--operation-id", required=True)
    verification.add_argument("--target-release", required=True)
    observation = commands.add_parser(
        "maintenance-observe", help="check exact activated schema/history without starting or stopping the server"
    )
    observation.add_argument("--operation-id", required=True)
    observation.add_argument("--target-release", required=True)
    recovery_prepare = commands.add_parser(
        "recovery-prepare", help="restore new fenced replacement state without SQL migration or server startup"
    )
    recovery_prepare.add_argument("--backup", required=True, type=Path)
    recovery_prepare.add_argument("--key-file", required=True, type=Path)
    recovery_prepare.add_argument("--expected-sha256", required=True)
    recovery_prepare.add_argument("--operation-id", required=True)
    recovery_prepare.add_argument("--target-release", required=True)
    recovery_prepare.add_argument("--confirm-source-stopped", required=True, action="store_true")
    recovery_prepare.add_argument("--maximum-bytes", type=int, default=DEFAULT_BYTES)
    recovery_prepare.add_argument("--mail-file", type=Path)
    recovery_prepare.add_argument("--expected-mail-sha256")
    for name in ("rollback-capture", "rollback-restore"):
        command = commands.add_parser(
            name,
            help="fence and verify displaced state" if name == "rollback-capture" else "prepare a fenced source restoration in new state",
        )
        command.add_argument("--operation-id", required=True)
        command.add_argument("--rollback-id", required=True)
        command.add_argument("--plan-id", required=True)
        command.add_argument("--source-release", required=True)
        command.add_argument("--expected-sha256", required=True)
        command.add_argument("--confirm-source-stopped", action="store_true")
        command.add_argument("--confirm-newer-data-loss", action="store_true")
        command.add_argument("--maximum-bytes", type=int, default=DEFAULT_BYTES)
        if name == "rollback-capture":
            command.add_argument("--target-release", required=True)
            command.add_argument("--source-schema", type=int, required=True)
        else:
            command.add_argument("--backup", type=Path, required=True)
            command.add_argument("--key-file", type=Path, required=True)
            command.add_argument("--mail-file", type=Path)
            command.add_argument("--expected-mail-sha256")
    for name in ("rollback-verify", "rollback-release", "rollback-observe"):
        command = commands.add_parser(
            name,
            help="verify prepared source state"
            if name == "rollback-verify"
            else "permit confirmed source activation"
            if name == "rollback-release"
            else "verify released source schema",
        )
        command.add_argument("--rollback-id", required=True)
        command.add_argument("--source-release", required=True)
        if name == "rollback-release":
            command.add_argument("--confirm-activation", action="store_true")
    for command in ("backup-inspect", "restore"):
        operation = commands.add_parser(
            command, help="verify an encrypted backup" if command == "backup-inspect" else "restore into new state only"
        )
        operation.add_argument("--backup", type=Path, required=True)
        operation.add_argument("--key-file", type=Path, required=True)
        operation.add_argument("--expected-sha256", required=True)
        operation.add_argument("--maximum-bytes", type=int, default=DEFAULT_BYTES)
        if command == "restore":
            operation.add_argument(
                "--confirm-source-stopped", action="store_true", help="confirm the original server and workers are stopped/fenced"
            )
    setup = commands.add_parser("setup", help="create the initial owner and organization using host-local prompts")
    setup.add_argument("--email", required=True)
    setup.add_argument("--name", required=True)
    setup.add_argument("--organization", required=True)
    setup_file = commands.add_parser("setup-file", help="recoverable host-only initial owner setup from a private immutable input")
    setup_file.add_argument("--input", required=True, type=Path)
    setup_file.add_argument("--expected-sha256", required=True)
    recover = commands.add_parser("reset-password", help="host administrator recovery; revokes all account sessions")
    recover.add_argument("--email", required=True)
    mail = commands.add_parser("mail-config", help="write a private Resend mail configuration using a hidden credential prompt")
    mail.add_argument("--sender", required=True)
    mail.add_argument("--output", type=Path, required=True)
    mail.add_argument("--daily-limit", type=int, default=100)
    serve = commands.add_parser("serve", help="serve behind a same-origin HTTPS reverse proxy")
    serve.add_argument("--public-url", required=True)
    serve.add_argument("--deployment-release", help="operator-supplied release manifest digest for health-response identity")
    serve.add_argument("--maintenance-operation", help="exact original prepared migration operation; permits target activation/restart")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--frontend-dir", type=Path, help="built web/dist directory for the organization chat interface")
    serve.add_argument("--no-inference-worker", action="store_true", help="disable LLM submission and execution on this installation")
    serve.add_argument("--no-topic-events", action="store_true",
                       help="disable per-topic browser event streams; the browser keeps polling")
    serve.add_argument("--agent-concurrency", type=int, default=8, help="simultaneous native topic agents (1–256; default 8)")
    serve.add_argument("--compute-images", type=Path, help="operator-approved digest-pinned compute image catalog")
    serve.add_argument("--mail-config", type=Path, help="private mail configuration; defaults to DATA_DIR/mail.json if present")
    serve.add_argument(
        "--openrouter-url", default="https://openrouter.ai/api/v1", help="canonical endpoint or loopback provider test fixture"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "mail-config":
            from reasoning_ledger.deployment.private import write_private

            key = getpass.getpass("Resend sending API key: ")
            configuration = MailConfig(args.sender, key, args.daily_limit)
            write_private(args.output, (json.dumps(configuration.document(), sort_keys=True, separators=(",", ":")) + "\n").encode())
            print(json.dumps({"configuration_file": str(args.output), "provider": "resend", "creates_cloud_resources": False}))
            return 0
        if args.command == "backup-key":
            print(json.dumps(create_key(args.output)))
            return 0
        if args.command == "backup":
            print(json.dumps(backup(args.data_dir, args.output, args.key_file, maximum=args.maximum_bytes)))
            return 0
        if args.command == "backup-inspect":
            print(json.dumps(inspect_backup(args.backup, args.key_file, args.expected_sha256, maximum=args.maximum_bytes)))
            return 0
        if args.command == "recovery-prepare":
            print(
                json.dumps(
                    recovery.prepare(
                        args.backup,
                        args.key_file,
                        args.expected_sha256,
                        args.data_dir,
                        operation_id=args.operation_id,
                        target_release=args.target_release,
                        source_stopped=args.confirm_source_stopped,
                        maximum=args.maximum_bytes,
                        mail_file=args.mail_file,
                        expected_mail=args.expected_mail_sha256,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "migrate-offline":
            print(
                json.dumps(
                    migrate(
                        args.data_dir,
                        args.backup,
                        args.key_file,
                        args.expected_sha256,
                        source_schema=args.source_schema,
                        target_release=args.target_release,
                        operation_id=args.operation_id,
                        source_stopped=args.confirm_source_stopped,
                        maximum=args.maximum_bytes,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "maintenance-verify":
            print(
                json.dumps(
                    verify_prepared(args.data_dir, operation_id=args.operation_id, target_release=args.target_release), sort_keys=True
                )
            )
            return 0
        if args.command == "maintenance-observe":
            print(
                json.dumps(
                    observe_activation(args.data_dir, operation_id=args.operation_id, target_release=args.target_release), sort_keys=True
                )
            )
            return 0
        if args.command == "maintenance-finish":
            print(
                json.dumps(
                    finish(
                        args.data_dir,
                        operation_id=args.operation_id,
                        target_release=args.target_release,
                        activation_verified=args.confirm_activation_verified,
                    ),
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "restore":
            print(
                json.dumps(
                    restore(
                        args.backup,
                        args.key_file,
                        args.expected_sha256,
                        args.data_dir,
                        source_stopped=args.confirm_source_stopped,
                        maximum=args.maximum_bytes,
                    )
                )
            )
            return 0
        if args.command in {"rollback-capture", "rollback-restore"}:
            options = {
                "operation_id": args.operation_id,
                "rollback_id": args.rollback_id,
                "plan_id": args.plan_id,
                "source_release": args.source_release,
                "source_stopped": args.confirm_source_stopped,
                "data_loss_confirmed": args.confirm_newer_data_loss,
                "maximum": args.maximum_bytes,
            }
            if args.command == "rollback-capture":
                receipt = rollback.capture(
                    args.data_dir,
                    target_release=args.target_release,
                    source_schema=args.source_schema,
                    expected=args.expected_sha256,
                    **options,
                )
            else:
                receipt = rollback.restore_prepared(
                    args.backup,
                    args.key_file,
                    args.expected_sha256,
                    args.data_dir,
                    mail_file=args.mail_file,
                    expected_mail=args.expected_mail_sha256,
                    **options,
                )
            print(json.dumps(receipt, sort_keys=True))
            return 0
        if args.command in {"rollback-verify", "rollback-release", "rollback-observe"}:
            options = {"rollback_id": args.rollback_id, "source_release": args.source_release}
            if args.command == "rollback-release":
                receipt = rollback.release(args.data_dir, activation_confirmed=args.confirm_activation, **options)
            elif args.command == "rollback-verify":
                receipt = rollback.verify_prepared(args.data_dir, **options)
            else:
                receipt = rollback.observe(args.data_dir, **options)
            print(json.dumps(receipt, sort_keys=True))
            return 0
        if args.command == "serve":
            mail_path = args.mail_config or args.data_dir / "mail.json"
            mail = MailConfig.read(mail_path) if args.mail_config or mail_path.exists() or mail_path.is_symlink() else None
            config = Config(
                args.data_dir,
                args.public_url,
                args.frontend_dir,
                not args.no_inference_worker,
                args.openrouter_url,
                args.compute_images,
                mail,
                args.deployment_release,
                args.agent_concurrency,
                not args.no_topic_events,
            )
            # Only the host-local reverse proxy can supply forwarded client IPs.
            # All application origin checks use public-url, not forwarded headers.
            with serving(args.data_dir, release=args.deployment_release, operation_id=args.maintenance_operation):
                app = create_app(config)
                uvicorn.run(app, host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="127.0.0.1,::1", access_log=False)
            return 0
        if args.command == "setup-file":
            from reasoning_ledger.deployment.private import read_private

            from .onboarding import MAX_OWNER_INPUT, canonical_owner, owner_input

            raw = read_private(args.input, MAX_OWNER_INPUT)
            if hashlib.sha256(raw).hexdigest() != args.expected_sha256:
                raise ValueError("private owner input does not match the expected SHA-256")
            value = owner_input(json.loads(raw))
            if canonical_owner(value) != raw:
                raise ValueError("private owner input must be canonical JSON")
            store = Store(args.data_dir)
            print(json.dumps(store.bootstrap_host(store._setup_token, value), sort_keys=True))
            return 0
        store = Store(args.data_dir)
        if args.command == "init":
            print(
                json.dumps(
                    {
                        "data_dir": str(store.data_dir),
                        "initialized": store.initialized(),
                        "setup_token_file": str(store.data_dir / "setup-token"),
                    }
                )
            )
            return 0
        password = getpass.getpass("New password (at least 12 characters): ")
        if password != getpass.getpass("Confirm password: "):
            raise ValueError("passwords do not match")
        if args.command == "reset-password":
            store.reset_password(args.email, password)
            print("Password reset; all account sessions revoked.")
        else:
            body = Bootstrap(
                email=args.email,
                name=args.name,
                organization_name=args.organization,
                password=password,
                setup_token=(store.data_dir / "setup-token").read_text(),
            )
            result = store.bootstrap(
                body.setup_token.get_secret_value(), body.email, body.name, body.password.get_secret_value(), body.organization_name
            )
            store.logout(result["access_token"])
            print(json.dumps({"organization_id": result["organization_id"], "user_id": result["user_id"]}))
        return 0
    except (StoreError, ValueError, OSError, InstallError) as exc:
        # Pydantic validation messages may include the original secret input.
        from pydantic import ValidationError

        message = "invalid account details" if isinstance(exc, ValidationError) else str(exc)
        if args.command == "setup-file" and not isinstance(exc, StoreError):
            message = "invalid or unavailable private initial-owner input/state"
        parser.exit(2, f"reasoning-server: {message}\n")


if __name__ == "__main__":
    raise SystemExit(main())
