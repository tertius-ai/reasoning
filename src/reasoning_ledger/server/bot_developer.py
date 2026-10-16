"""Developer tools without shell privileges; exact, human-approved outbound effects.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import re
import time

from .security import digest
from .store import StoreError, _id, _row

REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
USER = re.compile(r"[a-f0-9]{32}\Z")
# Statuses of an issue decision that counts against a self-approving requester's rolling 24-hour limit.
SELF_APPROVED = ("approved", "consumed", "created", "outcome_unknown")
DAY = 86_400
DEFAULT_DAILY_LIMIT = 3


def attribution_label(name):
    """A display name as one inert Markdown code span: no mention, link or formatting reaches GitHub."""
    text = " ".join("".join(char if char.isprintable() and char != "`" else " " for char in str(name)).split())[:100]
    return "`" + (text or "unnamed") + "`"


class BotDeveloper:
    def __init__(self, runtime, *, deployment=None):
        self.runtime, self.store = runtime, runtime.store
        self.deployment = deployment or {"release_id": None, "source_revision": None}

    def configure(self, user, org, bot_id, enabled, repositories, requesters, issue_self_approvers=None,
                  issue_daily_limit=DEFAULT_DAILY_LIMIT):
        issue_self_approvers = [] if issue_self_approvers is None else issue_self_approvers
        if (type(enabled) is not bool or not isinstance(repositories, list) or len(repositories) > 20
                or any(not isinstance(repo, str) or not REPOSITORY.fullmatch(repo) for repo in repositories)
                or len(set(repositories)) != len(repositories) or not isinstance(requesters, list) or len(requesters) > 64
                or any(not isinstance(person, str) or not USER.fullmatch(person) for person in requesters)
                or len(set(requesters)) != len(requesters) or enabled and not repositories):
            raise StoreError(400, "choose exact GitHub owner/repository names and permitted organization requesters")
        if (not isinstance(issue_self_approvers, list) or len(set(map(str, issue_self_approvers))) != len(issue_self_approvers)
                or any(person not in requesters for person in issue_self_approvers)
                or type(issue_daily_limit) is not int or not 1 <= issue_daily_limit <= 20):
            raise StoreError(400, "requesters who approve their own issues must be permitted requesters, "
                                  "with a daily limit from 1 to 20")
        with self.store.transaction(write=True) as db:
            bot = self.runtime.bots._manage(db, user, org, bot_id, True)
            self.runtime.bots._authority(db, bot)
            self.store._require(db, user, org, write=True)
            for person in requesters:
                self.store._require(db, person, org, write=True)
            if bot["created_by"] in issue_self_approvers:
                raise StoreError(400, "the bot owner already approves issues; choose other permitted requesters")
            db.execute("INSERT INTO bot_developer_permissions(bot_id,repositories_json,requesters_json,enabled,"
                       "issue_self_approvers_json,issue_daily_limit) VALUES(?,?,?,?,?,?) ON CONFLICT(bot_id) DO UPDATE SET "
                       "repositories_json=excluded.repositories_json,requesters_json=excluded.requesters_json,"
                       "enabled=excluded.enabled,issue_self_approvers_json=excluded.issue_self_approvers_json,"
                       "issue_daily_limit=excluded.issue_daily_limit",
                       (bot_id, json.dumps(repositories), json.dumps(requesters), enabled, json.dumps(issue_self_approvers),
                        issue_daily_limit))
            db.execute("UPDATE bot_issue_approvals SET status='denied',decided_at=? WHERE bot_id=? AND status IN ('pending','approved')",
                       (int(time.time()), bot_id))
            db.execute("UPDATE bot_execution_approvals SET status='denied',decided_at=? "
                       "WHERE bot_id=? AND status IN ('pending','approved') AND command_json LIKE ?",
                       (int(time.time()), bot_id, '["reasoning-dev-test",%'))
            self.store._audit(db, org, user, "bot.developer.permissions", bot_id)
            return {"enabled": enabled, "repositories": repositories, "requesters": requesters,
                    "issue_self_approvers": issue_self_approvers, "issue_daily_limit": issue_daily_limit}

    @staticmethod
    def permission(db, bot, run):
        grant = db.execute("SELECT * FROM bot_developer_permissions WHERE bot_id=?", (bot["id"],)).fetchone()
        if not grant or not grant["enabled"]:
            raise StoreError(403, "developer tools are disabled for this bot")
        if run["requested_by"] != bot["created_by"] and run["requested_by"] not in json.loads(grant["requesters_json"]):
            raise StoreError(403, "this requester has not been permitted to use developer tools")
        return grant

    @staticmethod
    def _self_approved(db, bot_id, person, now):
        return db.execute("SELECT COUNT(*) FROM bot_issue_approvals WHERE bot_id=? AND approver_id=? AND decided_by=? "
                          "AND status IN (?,?,?,?) AND decided_at>=?", (bot_id, person, person, *SELF_APPROVED, now - DAY)
                          ).fetchone()[0]

    @staticmethod
    def _approver(approval):
        return approval["approver_id"] or approval["owner_id"]

    def _approver_access(self, db, bot, run, approval, grant):
        """A requester approves only their own request, while the owner's live grant still names them."""
        approver = self._approver(approval)
        if approver == bot["created_by"]:
            return
        if approver != run["requested_by"] or approver not in json.loads(grant["issue_self_approvers_json"]):
            raise StoreError(403, "the bot owner no longer lets this requester approve their own issues")

    def authorized(self, db, credential, run_id, lease):
        bot, run = self.runtime.leased(db, credential, run_id, lease)
        grant = self.permission(db, bot, run)
        self.store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"], write=True)
        return bot, run, grant

    def context(self, credential, run_id, lease):
        with self.store.transaction() as db:
            bot, run = self.runtime.leased(db, credential, run_id, lease)
            try:
                grant = self.permission(db, bot, run)
            except StoreError:
                return {"enabled": False}
            return {"enabled": True, "repositories": json.loads(grant["repositories_json"])}

    def diagnostics(self, credential, run_id, lease):
        from .bot_diagnostics import collect

        with self.store.transaction() as db:
            _, run, _ = self.authorized(db, credential, run_id, lease)
            return {**collect(db, run["organization_id"], run["topic_id"]), "deployment": self.deployment}

    def request(self, credential, run_id, lease, repository, title, body, client_id):
        if (not isinstance(repository, str) or not REPOSITORY.fullmatch(repository) or not isinstance(title, str)
                or not title.strip() or len(title) > 200 or "\n" in title or "\r" in title or not isinstance(body, str)
                or not body.strip() or len(body) > 16000 or "\0" in title + body
                or not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", client_id)):
            raise StoreError(400, "provide a bounded exact repository, title, issue body and request identity")
        requested = digest(json.dumps({"repository": repository, "title": title, "body": body}, sort_keys=True))
        with self.store.transaction(write=True) as db:
            bot, run, grant = self.authorized(db, credential, run_id, lease)
            if repository not in json.loads(grant["repositories_json"]):
                raise StoreError(403, "repository is not in the owner's developer allowlist")
            previous = db.execute("SELECT * FROM bot_issue_approvals WHERE run_id=? AND client_id=?", (run_id, client_id)).fetchone()
            if previous:
                if (previous["request_sha256"] or previous["document_sha256"]) != requested:
                    raise StoreError(409, "issue request identity was reused with different inputs")
                return dict(previous)
            if db.execute("SELECT COUNT(*) FROM bot_issue_approvals WHERE run_id=?", (run_id,)).fetchone()[0] >= 10:
                raise StoreError(429, "this invocation has ten issue requests; inspect the existing approvals")
            identity, now = _id(), int(time.time())
            requester, approver = run["requested_by"], bot["created_by"]
            published = body
            if requester != bot["created_by"]:
                # Server-generated attribution: GitHub shows the owner's account as author.
                names = dict(db.execute("SELECT id,name FROM users WHERE id IN (?,?)", (requester, bot["id"])).fetchall())
                published = (body + "\n\n---\n\nRequested in Reasoning by " + attribution_label(names.get(requester, ""))
                             + " through the bot " + attribution_label(names.get(bot["id"], "")) + ".")
                waiting = db.execute("SELECT COUNT(*) FROM bot_issue_approvals WHERE bot_id=? AND approver_id=? "
                                     "AND status='pending'", (bot["id"], requester)).fetchone()[0]
                if (requester in json.loads(grant["issue_self_approvers_json"])
                        and self._self_approved(db, bot["id"], requester, now) + waiting < grant["issue_daily_limit"]):
                    approver = requester
            document = json.dumps({"repository": repository, "title": title, "body": published}, sort_keys=True)
            db.execute("INSERT INTO bot_issue_approvals(id,run_id,bot_id,owner_id,repository,title,body,"
                       "document_sha256,client_id,created_at,approver_id,request_sha256) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (identity, run_id, bot["id"], bot["created_by"], repository, title,
                                                          published, digest(document), client_id, now, approver, requested))
            db.execute("UPDATE bot_runs SET status='waiting_approval',phase='waiting_approval',updated_at=? WHERE id=?", (now, run_id))
            return dict(db.execute("SELECT * FROM bot_issue_approvals WHERE id=?", (identity,)).fetchone())

    def approvals(self, user, org):
        """Requests this person decides, and decided requests for bots they own (who approved is retained)."""
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            records = []
            for row in db.execute("SELECT a.*,r.topic_id,u.name AS requester_name,t.name AS topic_name,"
                "d.name AS decided_by_name FROM bot_issue_approvals a "
                "JOIN bot_runs r ON r.id=a.run_id JOIN users u ON u.id=r.requested_by JOIN topics t ON t.id=r.topic_id "
                "LEFT JOIN users d ON d.id=a.decided_by "
                "WHERE (a.owner_id=? OR a.approver_id=?) AND r.organization_id=? "
                "AND a.status IN ('pending','approved','consumed','outcome_unknown','created') "
                "ORDER BY a.created_at DESC LIMIT 50", (user, user, org)):
                approver = self._approver(row)
                try:
                    self.store._topic_access(db, user, org, row["topic_id"])
                    run = db.execute("SELECT * FROM bot_runs WHERE id=?", (row["run_id"],)).fetchone()
                    if row["status"] in {"pending", "approved"}:
                        if approver != user:
                            continue
                        bot = db.execute("SELECT * FROM bots WHERE id=?", (row["bot_id"],)).fetchone()
                        self.runtime._run_access(db, bot, run)
                        self._approver_access(db, bot, run, row, self.permission(db, bot, run))
                        if run["status"] != "waiting_approval":
                            continue
                except StoreError:
                    continue
                records.append({**dict(row), "delivery_interrupted": run["error_code"] == "approval_transport_ended",
                                "approval_role": "owner" if approver == row["owner_id"] else "requester"})
            return records

    def approve(self, user, org, approval_id, allow, *, request_sha256=None):
        if type(allow) is not bool:
            raise StoreError(400, "choose an explicit issue approval decision")
        with self.store.transaction(write=True) as db:
            approval = _row(db.execute("SELECT a.* FROM bot_issue_approvals a JOIN bot_runs r ON r.id=a.run_id "
                "WHERE a.id=? AND COALESCE(a.approver_id,a.owner_id)=? AND r.organization_id=?",
                (approval_id, user, org)).fetchone())
            bot = db.execute("SELECT * FROM bots WHERE id=?", (approval["bot_id"],)).fetchone()
            run = db.execute("SELECT * FROM bot_runs WHERE id=?", (approval["run_id"],)).fetchone()
            self.runtime._run_access(db, bot, run)
            grant = self.permission(db, bot, run)
            self._approver_access(db, bot, run, approval, grant)
            self._exact_request(bot, approval, request_sha256=request_sha256)
            if allow and run["error_code"] == "approval_transport_ended":
                raise StoreError(409, "bot transport ended before publication; stop this invocation and submit a new request")
            withdraw = not allow and approval["status"] == "approved" and run["error_code"] == "approval_transport_ended"
            if (approval["status"] != "pending" and not withdraw) or run["status"] != "waiting_approval":
                raise StoreError(409, "issue request is no longer awaiting consent")
            now = int(time.time())
            if allow and user != bot["created_by"] and self._self_approved(db, bot["id"], user, now) >= grant["issue_daily_limit"]:
                raise StoreError(429, f"you have reached the bot owner's limit of {grant['issue_daily_limit']} self-approved "
                                      "GitHub issues in 24 hours; reject this request or ask the bot owner")
            db.execute("UPDATE bot_issue_approvals SET status=?,decided_at=?,decided_by=? WHERE id=?",
                       ("approved" if allow else "denied", now, user, approval_id))
            self.store._audit(db, org, user, "bot.issue.approve" if allow else "bot.issue.deny", approval_id)
            return {"status": "approved" if allow else "denied"}

    def receipt(self, credential, run_id, lease, approval_id, *, consume=False, issue_url=None, unknown=False):
        with self.store.transaction(write=consume or issue_url is not None or unknown) as db:
            bot, run, grant = self.authorized(db, credential, run_id, lease)
            approval = _row(db.execute("SELECT * FROM bot_issue_approvals WHERE id=? AND run_id=? AND bot_id=?",
                                       (approval_id, run_id, bot["id"])).fetchone())
            if approval["repository"] not in json.loads(grant["repositories_json"]):
                raise StoreError(403, "repository developer permission changed")
            if consume:
                if run["error_code"] == "approval_transport_ended":
                    raise StoreError(409, "bot transport ended before publication; this consent cannot be consumed")
                self._exact_request(bot, approval)
                self._approver_access(db, bot, run, approval, grant)
                if approval["status"] != "approved" or approval["decided_by"] not in {None, self._approver(approval)}:
                    raise StoreError(409, "issue approval is not available; never retry an unknown outbound effect")
                db.execute("UPDATE bot_issue_approvals SET status='consumed',consumed_at=? WHERE id=?", (int(time.time()), approval_id))
                db.execute("UPDATE bot_runs SET status='running',phase='using_tools',updated_at=? WHERE id=?", (int(time.time()), run_id))
            elif issue_url is not None or unknown:
                if approval["status"] not in {"consumed", "outcome_unknown", "created"}:
                    raise StoreError(409, "issue approval has not been consumed")
                expected = r"https://github\.com/" + re.escape(approval["repository"]) + r"/issues/[1-9][0-9]*\Z"
                if issue_url is not None and (not isinstance(issue_url, str) or not re.fullmatch(expected, issue_url, flags=re.IGNORECASE)):
                    raise StoreError(400, "issue receipt URL differs from the approved repository")
                if approval["status"] == "created" and approval["issue_url"] != issue_url:
                    raise StoreError(409, "issue completion differs from its retained receipt")
                db.execute("UPDATE bot_issue_approvals SET status=?,issue_url=? WHERE id=?",
                           ("created" if issue_url else "outcome_unknown", issue_url, approval_id))
            return dict(db.execute("SELECT * FROM bot_issue_approvals WHERE id=?", (approval_id,)).fetchone())

    @staticmethod
    def _exact_request(bot, approval, *, request_sha256=None):
        document = json.dumps({key: approval[key] for key in ("repository", "title", "body")}, sort_keys=True)
        if (approval["bot_id"] != bot["id"] or approval["owner_id"] != bot["created_by"]
                or digest(document) != approval["document_sha256"]
                or request_sha256 is not None and request_sha256 != approval["document_sha256"]):
            raise StoreError(409, "issue request changed; review its exact action again")
