"""Human-owner cloud bot wizard; never available to bot bearer credentials.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import base64
import hashlib
import json
import secrets
import string
import threading
import time
from typing import Annotated, Literal

from fastapi import BackgroundTasks, Depends, FastAPI
from pydantic import Field, SecretStr

from ..deployment import bot_cloud
from ..deployment.digitalocean import DigitalOcean
from ..deployment.host import InstallError
from ..deployment.private import locked_directory, read_document, write_document, write_private
from ..deployment.setup import current_public_ipv4
from .bot_store import BotStore
from .request_input import RequestInput
from .security import digest
from .store import StoreError


class QuoteInput(RequestInput):
    token: SecretStr
    name: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,40}$")]
    engine: Literal["codex", "claude"]
    models: Annotated[list[Annotated[str, Field(max_length=100)]], Field(min_length=1, max_length=20)] = Field(
        default_factory=lambda: ["default"])
    region: Annotated[str, Field(pattern=r"^[a-z0-9]{2,12}$")] = "fra1"
    size: Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")] = "s-1vcpu-2gb"
    maximum_hourly_usd: Annotated[str, Field(pattern=r"^[0-9]{1,3}(\.[0-9]{1,6})?$")]
    cas: bool = False
    lean: bool = False


class ConfirmInput(RequestInput):
    confirmation: Annotated[str, Field(max_length=120)]


class CloudBots:
    def __init__(self, store, config):
        self.store, self.config = store, config
        self.bots = BotStore(store)
        self.capacity = threading.BoundedSemaphore(2)

    def require(self, user, org):
        with self.store.transaction() as db:
            self.store._require(db, user, org, write=True)
            if db.execute("SELECT account_kind FROM users WHERE id=?", (user,)).fetchone()[0] != "human":
                raise StoreError(403, "cloud provisioning requires a human owner")

    def directory(self, user, org, identifier=None):
        # IDs come from authenticated state and strict hex path checks.
        import re

        if any(not re.fullmatch(r"[a-f0-9]{32}", value) for value in (user, org, *((identifier,) if identifier else ()))):
            raise StoreError(404, "cloud bot not found")
        root = self.store.data_dir / "cloud-bots"
        for part in (org, user):
            with locked_directory(root):
                pass
            root = root / part
        with locked_directory(root):
            pass
        return root / identifier if identifier else root

    def read(self, user, org, identifier):
        self.require(user, org)
        path = self.directory(user, org, identifier) / "job.json"
        if not path.exists():
            raise StoreError(404, "cloud bot not found")
        return read_document(path)

    def credentials(self, job, secret=None):
        scope = {"organization_id": job["org"], "provider": "cloud-bot-owner-" + job["user"], "key_id": job["id"]}
        if secret is not None:
            return base64.b64encode(self.store._box.seal(json.dumps(secret), **scope)).decode()
        return json.loads(self.store._box.open(base64.b64decode(job["credentials"]), **scope))

    def public(self, job):
        value = {key: job[key] for key in ("id", "name", "engine", "phase", "quote", "created_at", "bot_id", "error",
                                         "console_url", "login_command", "tools")}
        if job["phase"] in {"provisioning", "installing", "starting"}:
            try:
                with locked_directory(self.directory(job["user"], job["org"], job["id"]) / "operation"):
                    value["phase"] = "start_failed" if job["phase"] == "starting" else "setup_failed"
                    value["error"] = "Setup was interrupted. Resume this same operation; do not create a second VM."
            except InstallError:
                pass  # The owner job is still running in another thread/process.
        return value

    def quote(self, user, org, body):
        self.require(user, org)
        wheel = bot_cloud.wheel_for_release(self.config.deployment_release)
        spec = {"server": self.config.public_url, "engine": body.engine, "models": body.models, "cas": body.cas,
                "lean": body.lean,
                "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
        bot_cloud.validate_spec(spec)
        provider = DigitalOcean(body.token.get_secret_value())
        try:
            quote = provider.compute_quote(name=body.name, region=body.region, size=body.size,
                                           admin_cidr=current_public_ipv4(), maximum_hourly_usd=body.maximum_hourly_usd)
            bot_cloud.check_size(quote, spec)
        finally:
            provider.close()
        root = self.directory(user, org)
        with locked_directory(root):
            jobs = list(root.glob("*/job.json"))
            if len(jobs) >= 20:
                raise StoreError(409, "cloud bot setup limit reached; use the existing resumable jobs")
            identifier = secrets.token_hex(16)
            job = {"id": identifier, "user": user, "org": org, "name": body.name, "engine": body.engine,
                   "phase": "quoted", "quote": quote, "created_at": int(time.time()), "bot_id": None,
                   "spec": spec, "wheel": wheel.name, "error": None, "console_url": None,
                   "login_command": bot_cloud.login_command(body.engine),
                   "tools": [*( ["SymPy", "GAP", "PARI/GP", "Singular"] if body.cas else []),
                             *( ["Lean 4", "mathlib"] if body.lean else [])]}
            job["credentials"] = self.credentials(job, {
                "digitalocean": body.token.get_secret_value(), "bot": "rnb_" + secrets.token_urlsafe(32),
                "console_password": "".join(secrets.choice(string.ascii_lowercase) for _ in range(32)),
            })
            with locked_directory(root / identifier) as directory:
                with locked_directory(directory / "artifact") as artifact:
                    write_private(artifact / wheel.name, wheel.read_bytes(), maximum=32 * 1024**2)
                write_document(directory / "job.json", job, new=True)
        return self.public(job)

    def ensure_bot(self, job, credential):
        with self.store.transaction(write=True) as db:
            self.store._require(db, job["user"], job["org"], write=True)
            row = db.execute("SELECT id FROM bots WHERE token_hash=? AND organization_id=? AND created_by=?",
                             (digest(credential), job["org"], job["user"])).fetchone()
            bot = self.bots._manage(db, job["user"], job["org"], row[0], True) if row else None
            if bot and (bot["revoked_at"] is not None or bot["expires_at"] <= int(time.time())):
                raise StoreError(403, "cloud bot access expired or was revoked; provisioning stopped")
            return row[0] if row else self.bots.create(job["user"], job["org"], job["name"], [], bot_cloud.SCOPES,
                lifetime=31_536_000, personal=True, public_access=True, _db=db, _credential_hash=digest(credential))["id"]

    def launch(self, user, org, identifier, action, confirmation, tasks):
        job = self.read(user, org, identifier)
        job["phase"] = self.public(job)["phase"]
        if confirmation != ("create bot " + job["name"] if action == "create" else "start bot " + job["name"]):
            raise StoreError(400, "type the exact cloud bot confirmation")
        if job["phase"] == "ready":
            return self.public(job)
        if action == "start" and job["phase"] not in {"login_required", "start_failed"}:
            raise StoreError(409, "finish provisioning and provider sign-in first")
        if not self.capacity.acquire(blocking=False):
            raise StoreError(409, "two cloud setups are already active; retry shortly")
        tasks.add_task(self.perform, user, org, identifier, action)
        return {**self.public(job), "phase": "starting" if action == "start" else "provisioning"}

    def perform(self, user, org, identifier, action):
        provider = None
        try:
            directory = self.directory(user, org, identifier)
            # Cross-process ownership fence, separate from the resource journal.
            with locked_directory(directory / "operation"):
                job = self.read(user, org, identifier)
                if job["phase"] == "ready":
                    return
                secret = self.credentials(job)
                provider = DigitalOcean(secret["digitalocean"])
                job["phase"], job["error"] = "starting" if action == "start" else "provisioning", None
                write_document(directory / "job.json", job)
                host = directory / "host"
                with locked_directory(host):
                    document = bot_cloud.prepare(provider, host, job["quote"], job["spec"])
                job["bot_id"] = self.ensure_bot(job, secret["bot"])
                write_document(directory / "job.json", job)

                def authorize():
                    self.require(user, org)
                    self.bots.identity(secret["bot"])

                controller = bot_cloud.BotProvisioner(provider, document, authorize=authorize)
                if action == "start":
                    bot_cloud.start(controller, host)
                    job["phase"] = "ready"
                    secret.pop("console_password", None)
                    job["credentials"] = self.credentials(job, secret)
                else:
                    result = bot_cloud.provision_ready(controller, host)
                    job["console_url"] = result["console_url"]
                    if result["ssh_host"]:
                        job["phase"] = "installing"
                        write_document(directory / "job.json", job)
                        bot_cloud.install(controller, host, directory / "artifact" / job["wheel"], secret["bot"],
                                          console_password=secret["console_password"])
                        job["phase"] = "login_required"
                    else:
                        job["phase"] = "waiting_for_vm"
                write_document(directory / "job.json", job)
        except (InstallError, StoreError, OSError, ValueError) as exc:
            # No provider bodies, command stderr, credential material, or
            # filesystem paths enter browser responses or application logs.
            if "job" in locals():
                job["phase"] = "start_failed" if action == "start" else "setup_failed"
                job["error"] = str(exc) if isinstance(exc, InstallError) else "Owner access or private setup state unavailable."
                write_document(directory / "job.json", job)
        finally:
            if provider:
                provider.close()
            self.capacity.release()


def install_routes(app: FastAPI, store, config, authenticated):
    controller = CloudBots(store, config)
    app.state.cloud_bots = controller
    User = Annotated[str, Depends(authenticated)]
    root = "/api/organizations/{organization_id}/my-cloud-bots"

    @app.get(root)
    def listing(organization_id: str, user: User):
        controller.require(user, organization_id)
        directory = controller.directory(user, organization_id)
        return [controller.public(read_document(path)) for path in sorted(directory.glob("*/job.json"))]

    @app.post(root + "/quote", status_code=201)
    def quote(organization_id: str, body: QuoteInput, user: User):
        try:
            return controller.quote(user, organization_id, body)
        except InstallError as exc:
            raise StoreError(400, str(exc)) from exc

    @app.post(root + "/{identifier}/console-access")
    def console_access(organization_id: str, identifier: str, user: User):
        job = controller.read(user, organization_id, identifier)
        if job["phase"] not in {"login_required", "start_failed"}:
            raise StoreError(409, "setup console access is available only while provider sign-in is required")
        secret = controller.credentials(job)
        if not secret.get("console_password"):
            raise StoreError(409, "setup console password has already been retired")
        with store.transaction(write=True) as db:
            store._require(db, user, organization_id, write=True)
            store._audit(db, organization_id, user, "bot.cloud_console_read", identifier)
        return {"username": "reasoning-bot", "password": secret["console_password"],
                "command": bot_cloud.LOGIN[job["engine"]]}

    @app.post(root + "/{identifier}/{action}", status_code=202)
    def launch(organization_id: str, identifier: str, action: Literal["create", "start"], body: ConfirmInput,
               tasks: BackgroundTasks, user: User):
        return controller.launch(user, organization_id, identifier, action, body.confirmation, tasks)
