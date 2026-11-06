"""Owner-approved bot computation using the shared immutable image queue.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json
import time

from .compute_catalog import canonical
from .security import digest
from .store import StoreError, _id, _row


class BotCompute:
    def __init__(self, runtime, compute):
        self.runtime, self.store, self.compute = runtime, runtime.store, compute

    def request(self, credential, run_id, lease, image_id, language, program, resources):
        if self.compute is None:
            raise StoreError(409, "organization compute is not configured")
        parameters = self.compute.parameters(image_id, language, program, resources)
        body = canonical(parameters)
        with self.store.transaction(write=True) as db:
            bot, run = self.runtime.leased(db, credential, run_id, lease)
            image = self.compute._admission(db, run["requested_by"], run["organization_id"], run["topic_id"], parameters)
            self.compute.require_runnable(db, run["organization_id"], image_id, parameters["resources"])
            prior = db.execute("SELECT * FROM bot_compute_approvals WHERE run_id=? AND request_sha256=?",
                               (run_id, digest(body))).fetchone()
            if prior:
                return self.view(prior)
            identity = _id()
            db.execute("INSERT INTO bot_compute_approvals VALUES(?,?,?,?,?,?,?,?,'pending',NULL,?)",
                       (identity, run_id, bot["id"], run["organization_id"], bot["created_by"], body, digest(body),
                        image.reference.split("@", 1)[1], int(time.time())))
            self.store._audit(db, run["organization_id"], run["requested_by"], "bot.compute.request", identity)
            return self.view(db.execute("SELECT * FROM bot_compute_approvals WHERE id=?", (identity,)).fetchone())

    @staticmethod
    def view(row):
        return {key: row[key] for key in ("id", "run_id", "bot_id", "status", "job_id", "request_sha256", "image_digest", "created_at")}

    def approvals(self, user, org):
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            records = []
            for row in db.execute(
                "SELECT a.*,r.requested_by,r.topic_id,u.name AS requester_name,t.name AS topic_name "
                "FROM bot_compute_approvals a JOIN bot_runs r ON r.id=a.run_id JOIN users u ON u.id=r.requested_by "
                "JOIN topics t ON t.id=r.topic_id WHERE a.organization_id=? AND a.owner_id=? AND a.status='pending' "
                "ORDER BY a.created_at,a.id LIMIT 100", (org, user)):
                try:
                    run = db.execute("SELECT * FROM bot_runs WHERE id=?", (row["run_id"],)).fetchone()
                    bot = db.execute("SELECT * FROM bots WHERE id=?", (row["bot_id"],)).fetchone()
                    self.runtime._run_access(db, bot, run)
                    if run["status"] not in {"running", "waiting_approval", "succeeded"}:
                        continue
                except StoreError:
                    continue
                records.append(dict(row) | {"parameters": json.loads(row["parameters_json"])})
            return records

    def decide(self, user, org, identity, allow, *, request_sha256=None):
        if type(allow) is not bool:
            raise StoreError(400, "choose an explicit computation approval decision")
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org)
            row = _row(db.execute("SELECT * FROM bot_compute_approvals WHERE id=? AND organization_id=?",
                                   (identity, org)).fetchone())
            if row["owner_id"] != user:
                raise StoreError(403, "only the bot owner may approve this computation")
            parameters = json.loads(row["parameters_json"])
            if (digest(canonical(parameters)) != row["request_sha256"]
                    or request_sha256 is not None and request_sha256 != row["request_sha256"]):
                raise StoreError(409, "computation request changed; review its exact action again")
            if row["status"] != "pending":
                if allow and row["status"] != "submitted" or not allow and row["status"] != "denied":
                    raise StoreError(409, "compute decision differs from the retained receipt")
                return self.view(row)
            if allow:
                run = _row(db.execute("SELECT * FROM bot_runs WHERE id=?", (row["run_id"],)).fetchone())
                bot = _row(db.execute("SELECT * FROM bots WHERE id=?", (run["bot_id"],)).fetchone())
                self.runtime._run_access(db, bot, run)
                if run["status"] not in {"running", "waiting_approval", "succeeded"}:
                    raise StoreError(409, "the source bot run was stopped or failed")
                image = self.compute._admission(db, run["requested_by"], org, run["topic_id"], parameters)
                if image.reference.split("@", 1)[1] != row["image_digest"]:
                    raise StoreError(409, "approved compute image identity changed")
                self.compute.require_runnable(db, org, parameters["image_id"], parameters["resources"])
                job = self.compute._enqueue(db, run["requested_by"], org, run["topic_id"], "bot-compute-" + identity, parameters)
                db.execute("UPDATE bot_compute_approvals SET status='submitted',job_id=? WHERE id=?", (job["id"], identity))
            else:
                db.execute("UPDATE bot_compute_approvals SET status='denied' WHERE id=?", (identity,))
            self.store._audit(db, org, user, "bot.compute.approve" if allow else "bot.compute.deny", identity)
            return self.view(db.execute("SELECT * FROM bot_compute_approvals WHERE id=?", (identity,)).fetchone())
