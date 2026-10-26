"""Durable, topic-scoped local-agent work and owner-only execution consent.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

from __future__ import annotations

import json
import re
import time

from .bot_sharing import DECLINED, permits, require
from .bot_store import BotStore
from .security import digest, token
from .store import StoreError, _id, _row

MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}\Z")
ACTIVE = {"queued", "running", "waiting_approval"}
TERMINAL = {"succeeded", "failed", "outcome_unknown", "cancelled"}
# Error codes of a running invocation that a person, workflow or agent stop fenced.
STOPPED = {"stop_requested", "cancelled"}
PHASES = {"thinking", "reading", "writing", "using_tools", "waiting_approval", "finishing"}
AWAITING_CONSENT = (
    "(EXISTS (SELECT 1 FROM bot_execution_approvals a WHERE a.run_id=bot_runs.id AND a.status IN ('pending','approved')) "
    "OR EXISTS (SELECT 1 FROM bot_issue_approvals a WHERE a.run_id=bot_runs.id AND a.status IN ('pending','approved')))"
)
# Who must decide a run's pending GitHub issue request: the requester when the owner lets them approve their own.
APPROVAL_BY = (
    "(SELECT CASE WHEN a.approver_id IS NOT NULL AND a.approver_id<>a.owner_id THEN 'requester' ELSE 'owner' END "
    "FROM bot_issue_approvals a WHERE a.run_id=r.id AND a.status='pending' ORDER BY a.created_at DESC LIMIT 1) AS approval_by"
)
# Runner-reported stages: whole milliseconds on the runner's monotonic clock,
# measured from the runner's receipt of the claim response.
RUNNER_STAGES = ("cli_started_ms", "cli_ready_ms", "first_text_ms", "final_text_ms", "cli_exited_ms")
MENTION = re.compile(r"@\[([a-f0-9]{32})(?::([A-Za-z0-9][A-Za-z0-9._/-]{0,99})(?:!(low|medium|high|xhigh|max))?)?\]")


def mentioned_models(content, bot_id):
    return [match[2] for match in MENTION.finditer(content) if match[1] == bot_id]


def runner_timing(value):
    """Validate a runner's content-free stage report; absent stages stay unknown."""
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) - {*RUNNER_STAGES, "resolved_model"}
            or any(value.get(stage) is not None and (type(value[stage]) is not int or not 0 <= value[stage] <= 86_400_000)
                   for stage in RUNNER_STAGES)
            or value.get("resolved_model") is not None
            and (not isinstance(value["resolved_model"], str) or not MODEL.fullmatch(value["resolved_model"]))):
        raise StoreError(400, "invalid bot runner timing")
    return {key: value.get(key) for key in (*RUNNER_STAGES, "resolved_model")}


def bot_model(value):
    match = re.fullmatch(r"bot/([a-f0-9]{32})/(.+)", value or "")
    return (match[1], match[2]) if match and MODEL.fullmatch(match[2]) else None


class BotRuntime:
    def __init__(self, bots):
        self.bots, self.store = bots, bots.store

    @staticmethod
    def access(db, bot, topic, *, write=False):
        BotStore._authority(db, bot)
        if bot["revoked_at"] is not None or bot["expires_at"] <= int(time.time()):
            raise StoreError(403, "bot access is no longer active")
        scopes = set(json.loads(bot["scopes_json"]))
        if not {"messages:read", "messages:write" if write else "messages:read"}.issubset(scopes):
            raise StoreError(403, "bot lacks conversation access")
        if topic not in BotStore.topic_ids(db, bot):
            raise StoreError(404, "bot topic unavailable")
        return BotStore._personal_grants(db, bot["created_by"], bot["organization_id"], [topic], list(scopes))

    def advertise(self, credential, engine, models, allow_other_users, local_tools, capabilities=None):
        """``allow_other_users`` is the runner's flag, retained as reported; the bot's sharing setting decides access."""
        if engine not in {"codex", "claude"} or type(allow_other_users) is not bool or type(local_tools) is not bool:
            raise StoreError(400, "invalid local-agent policy")
        if (not isinstance(models, list) or not 1 <= len(models) <= 20 or len(set(models)) != len(models)
                or any(not isinstance(model, str) or not MODEL.fullmatch(model) for model in models)):
            raise StoreError(400, "advertise one to 20 distinct CLI model identifiers")
        self.bots._meter(credential, "read")
        if capabilities is not None and (not isinstance(capabilities, dict) or len(json.dumps(capabilities)) > 16000):
            raise StoreError(400, "invalid bounded bot capabilities")
        efforts = (capabilities or {}).get("efforts", {})
        if (not isinstance(efforts, dict) or set(efforts) - set(models)
                or any(not isinstance(values, list) or any(
                    not isinstance(value, str) or value not in {"low", "medium", "high", "xhigh", "max"} for value in values
                ) for values in efforts.values())):
            raise StoreError(400, "reasoning efforts must belong to advertised models and supported CLI levels")
        with self.store.transaction(write=True) as db:
            bot = self.bots._authenticate(db, credential)
            db.execute(
                "INSERT INTO bot_runtimes(bot_id,engine,models_json,allow_other_users,local_tools,last_seen_at,"
                "workspace_policy,capabilities_json) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(bot_id) DO UPDATE SET "
                "engine=excluded.engine,models_json=excluded.models_json,allow_other_users=excluded.allow_other_users,"
                "local_tools=excluded.local_tools,last_seen_at=excluded.last_seen_at,capabilities_json=excluded.capabilities_json",
                (bot["id"], engine, json.dumps(models), allow_other_users, local_tools, int(time.time()), "topic-v1",
                 json.dumps(capabilities or {})),
            )
            # mention_feedback: from_message records a visible refusal for a requester the sharing setting excludes.
            # run_timing: completions may carry the runner's content-free wait stages.
            # sharing: who may invoke the bot is its server-side setting, not the runner's flag.
            return {"models": models, "policy": "topic-v1", "mention_feedback": True, "run_timing": True, "sharing": "server"}

    def models(self, user, org, topic):
        with self.store.transaction() as db:
            self.store._topic_access(db, user, org, topic)
            values = []
            for row in db.execute("SELECT b.*,r.engine,r.models_json,r.last_seen_at,r.local_tools,r.capabilities_json "
                                  "FROM bots b JOIN bot_runtimes r ON r.bot_id=b.id WHERE b.organization_id=?", (org,)):
                try:
                    self.access(db, row, topic, write=True)
                except StoreError:
                    continue
                if not permits(db, row, user):
                    continue
                name = db.execute("SELECT name FROM users WHERE id=?", (row["id"],)).fetchone()[0]
                values.extend({"id": f"bot/{row['id']}/{model}", "name": f"{name} · {model}", "bot_id": row["id"],
                               "efforts": json.loads(row["capabilities_json"]).get("efforts", {}).get(model, []),
                               "engine": row["engine"], "owner_user_id": row["created_by"],
                               "local_tools": bool(row["local_tools"]),
                               "online": row["last_seen_at"] >= int(time.time()) - 45}
                              for model in json.loads(row["models_json"]))
            return values

    def channel_models(self, user, org, channel, *, db=None):
        """Routes whose following grants cover a newly created topic in this channel."""
        if db is None:
            with self.store.transaction() as transaction:
                return self.channel_models(user, org, channel, db=transaction)
        project = self.store._channel_access(db, user, org, channel, write=True)[1]
        values = []
        for row in db.execute("SELECT b.*,r.engine,r.models_json,r.last_seen_at,r.local_tools,r.capabilities_json "
                              "FROM bots b JOIN bot_runtimes r ON r.bot_id=b.id WHERE b.organization_id=?", (org,)):
            try:
                BotStore._authority(db, row)
                scopes = set(json.loads(row["scopes_json"]))
                if (not row["public_access"] or row["revoked_at"] is not None or row["expires_at"] <= int(time.time())
                    or not {"messages:read", "messages:write"}.issubset(scopes)
                    or not permits(db, row, user)):
                    continue
                if row["delegation_kind"] == "personal":
                    self.store._channel_access(db, row["created_by"], org, channel, write=True)
                elif project["visibility"] != "organization":
                    continue
            except StoreError:
                continue
            name = db.execute("SELECT name FROM users WHERE id=?", (row["id"],)).fetchone()[0]
            values.extend({"id": f"bot/{row['id']}/{model}", "name": f"{name} · {model}", "bot_id": row["id"],
                           "efforts": json.loads(row["capabilities_json"]).get("efforts", {}).get(model, []),
                           "engine": row["engine"], "owner_user_id": row["created_by"],
                           "local_tools": bool(row["local_tools"]),
                           "online": row["last_seen_at"] >= int(time.time()) - 45}
                          for model in json.loads(row["models_json"]))
        return values

    def enqueue(self, user, org, topic, model, client_id, *, through=None, prompt=None, db=None, mention=False, effort=None,
                decline=False):
        """Admit a bot request. With ``decline``, a requester the sharing setting excludes gets a visible "Not run" record."""
        if db is None:
            with self.store.transaction(write=True) as transaction:
                return self.enqueue(user, org, topic, model, client_id, through=through, prompt=prompt, db=transaction,
                                    mention=mention, effort=effort, decline=decline)
        parsed = bot_model(model)
        if parsed is None or not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            raise StoreError(400, "select a connected bot model and stable request identity")
        access = self.store._topic_access(db, user, org, topic, write=True)
        if access[4]["kind"] == "alpha" and not mention:
            raise StoreError(400, "bot inference requires a beta or gamma topic")
        bot = _row(db.execute("SELECT * FROM bots WHERE id=? AND organization_id=?", (parsed[0], org)).fetchone())
        self.access(db, bot, topic, write=True)
        runtime = _row(db.execute("SELECT * FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone())
        if not permits(db, bot, user):
            if not decline or prompt is not None:
                require(db, bot, user)
            previous = db.execute("SELECT * FROM bot_runs WHERE requested_by=? AND organization_id=? AND client_id=?",
                                  (user, org, client_id)).fetchone()
            if previous:
                if previous["topic_id"] != topic:
                    raise StoreError(409, "bot request identity was reused with different inputs")
                return self.public(previous)
            if type(through) is not int or db.execute(
                    "SELECT 1 FROM messages WHERE organization_id=? AND topic_id=? AND sequence=? AND sender_kind='human' "
                    "AND author_id=?", (org, topic, through, user)).fetchone() is None:
                raise StoreError(404, "human request message unavailable")
            return self._decline(db, bot, user, topic, through, client_id, parsed[1])
        if parsed[1] not in json.loads(runtime["models_json"]):
            raise StoreError(400, "this model was not advertised by the bot owner")
        from .reasoning_effort import checked

        checked(effort)
        if effort and effort not in json.loads(runtime["capabilities_json"]).get("efforts", {}).get(parsed[1], []):
            raise StoreError(400, "this bot model does not advertise that reasoning effort")
        identity = json.dumps({"model": model, "through": through, "prompt": prompt}, sort_keys=True)
        if effort:
            identity = json.dumps({"model": model, "through": through, "prompt": prompt, "effort": effort}, sort_keys=True)
        previous = db.execute("SELECT * FROM bot_runs WHERE requested_by=? AND organization_id=? AND client_id=?",
                              (user, org, client_id)).fetchone()
        if previous:
            if previous["request_json"] != identity or previous["topic_id"] != topic:
                raise StoreError(409, "bot request identity was reused with different inputs")
            return self.public(previous)
        if runtime["last_seen_at"] < int(time.time()) - 45:
            raise StoreError(409, "bot is offline; start its local runner before requesting inference")
        if through is not None and (type(through) is not int or through < 1):
            raise StoreError(400, "invalid conversation cursor")
        if prompt is not None and (not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 64000):
            raise StoreError(400, "workflow prompt must contain one to 64000 characters")
        if prompt is None:
            anchor = db.execute("SELECT * FROM messages WHERE organization_id=? AND topic_id=? AND sequence=? "
                                "AND sender_kind='human' AND author_id=?", (org, topic, through, user)).fetchone()
            if anchor is None:
                raise StoreError(404, "human request message unavailable")
            # The current request is bounded. Older conversation is fetched on demand, never organization-wide.
            prompt = anchor["content"]
        run_id, now = _id(), int(time.time())
        if db.execute("SELECT COUNT(*) FROM bot_runs WHERE bot_id=? AND status IN ('queued','running','waiting_approval')",
                      (bot["id"],)).fetchone()[0] >= 50:
            raise StoreError(409, "bot has 50 pending requests; finish or cancel existing work")
        db.execute("INSERT INTO bot_runs(id,organization_id,project_id,channel_id,topic_id,bot_id,requested_by,model,client_id,"
                   "request_json,prompt,status,phase,created_at,updated_at,context_through_sequence,enqueued_at_ms) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,'queued','thinking',?,?,?,?)",
                   (run_id, org, access[1]["id"], access[4]["channel_id"], topic, bot["id"], user, parsed[1], client_id,
                    identity, prompt, now, now, through, int(time.time() * 1000)))
        self.store._audit(db, org, user, "bot.inference.request", run_id)
        from .research_context import ResearchContext

        db.execute("UPDATE bot_runs SET effort=?,context_edit_sequence=? WHERE id=?",
                   (effort, ResearchContext.edit_sequence(db, org, topic), run_id))
        return self.public(db.execute("SELECT * FROM bot_runs WHERE id=?", (run_id,)).fetchone())

    def enqueue_direct(self, db, user, org, conversation_id, bot, message, model=None, effort=None):
        """Queue your own bot's answer to your `message` in your direct conversation with it.

        Unlike a topic request, an offline bot is not refused: the run waits, queued, until its runner reconnects."""
        runtime = db.execute("SELECT * FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone()
        if runtime is None:
            raise StoreError(409, "start this bot's runner once so it can list its models")
        models = json.loads(runtime["models_json"])
        model = model or (models[0] if models else None)
        if model not in models:
            raise StoreError(400, "this model was not advertised by the bot")
        from .reasoning_effort import checked

        checked(effort)
        if effort and effort not in json.loads(runtime["capabilities_json"]).get("efforts", {}).get(model, []):
            raise StoreError(400, "this bot model does not advertise that reasoning effort")
        if db.execute("SELECT COUNT(*) FROM bot_runs WHERE bot_id=? AND status IN ('queued','running','waiting_approval')",
                      (bot["id"],)).fetchone()[0] >= 50:
            raise StoreError(409, "bot has 50 pending requests; finish or cancel existing work")
        run_id, now = _id(), int(time.time())
        identity = json.dumps({"model": f"bot/{bot['id']}/{model}", "through": message["sequence"], "prompt": None,
                               "effort": effort, "direct_conversation_id": conversation_id}, sort_keys=True)
        db.execute("INSERT INTO bot_runs(id,organization_id,project_id,channel_id,topic_id,direct_conversation_id,bot_id,"
                   "requested_by,model,client_id,request_json,prompt,status,phase,created_at,updated_at,context_through_sequence,"
                   "enqueued_at_ms,effort) "
                   "VALUES(?,?,NULL,NULL,NULL,?,?,?,?,?,?,?,'queued','thinking',?,?,?,?,?)",
                   (run_id, org, conversation_id, bot["id"], user, model, "direct-" + message["id"], identity, message["content"],
                    now, now, message["sequence"], int(time.time() * 1000), effort))
        self.store._audit(db, org, user, "bot.inference.request", run_id)
        return self.public(db.execute("SELECT * FROM bot_runs WHERE id=?", (run_id,)).fetchone())

    def direct_listing(self, user, org, conversation_id):
        """Your bot's runs in your direct conversation with it, newest first."""
        from .direct_messages import DirectMessages

        with self.store.transaction(write=True) as db:
            DirectMessages.access(db, user, org, conversation_id)
            self.expire(db, org)
            return [self.public(row) for row in db.execute("SELECT r.*,u.name AS bot_name,b.created_by AS owner_user_id,"
                + APPROVAL_BY + " FROM bot_runs r JOIN users u ON u.id=r.bot_id JOIN bots b ON b.id=r.bot_id "
                "WHERE r.organization_id=? AND r.direct_conversation_id=? ORDER BY r.created_at DESC,r.id DESC LIMIT 50",
                (org, conversation_id))]

    def _direct_access(self, db, bot, run):
        if bot["revoked_at"] is not None or bot["expires_at"] <= int(time.time()):
            raise StoreError(403, "bot access is no longer active")
        if bot["created_by"] != run["requested_by"]:
            raise StoreError(403, "only a bot's owner can message it directly")
        self.store._require(db, run["requested_by"], run["organization_id"], write=True)
        first, second = sorted((run["requested_by"], bot["id"]))
        if db.execute("SELECT 1 FROM direct_conversations WHERE organization_id=? AND id=? AND first_user_id=? AND second_user_id=?",
                      (run["organization_id"], run["direct_conversation_id"], first, second)).fetchone() is None:
            raise StoreError(404, "direct conversation unavailable")

    @staticmethod
    def _direct_history(db, run):
        rows = db.execute("SELECT m.*,u.name,u.account_kind FROM direct_messages m JOIN users u ON u.id=m.author_id "
                          "WHERE m.organization_id=? AND m.conversation_id=? AND m.sequence<=? ORDER BY m.sequence DESC LIMIT 20",
                          (run["organization_id"], run["direct_conversation_id"], run["context_through_sequence"])).fetchall()
        return [{"id": row["id"], "sequence": row["sequence"], "sender": row["name"],
                 "kind": "bot" if row["account_kind"] == "bot" else "human", "text": row["content"]} for row in reversed(rows)]

    def from_message(self, credential, message_id):
        self.bots._meter(credential, "write")
        with self.store.transaction(write=True) as db:
            bot = self.bots._authenticate(db, credential)
            client_id = "mention-" + message_id + "-" + bot["id"]
            previous = db.execute("SELECT * FROM bot_runs WHERE organization_id=? AND client_id=? AND bot_id=?",
                                  (bot["organization_id"], client_id, bot["id"])).fetchone()
            if previous:
                if previous["error_code"] == DECLINED:
                    self.access(db, bot, previous["topic_id"])
                else:
                    self._run_access(db, bot, previous)
                return self.public(previous)
            message = _row(db.execute("SELECT * FROM messages WHERE id=? AND organization_id=? AND sender_kind='human'",
                                      (message_id, bot["organization_id"])).fetchone())
            message = self.store.original_message(db, message)
            selections = mentioned_models(message["content"], bot["id"])
            if not selections:
                raise StoreError(403, "this message does not mention the bot")
            runtime = _row(db.execute("SELECT * FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone())
            if not permits(db, bot, message["author_id"]):
                return self._decline(db, bot, message["author_id"], message["topic_id"], message["sequence"], client_id,
                                     (sorted(filter(None, selections)) or json.loads(runtime["models_json"])[:1])[0])
            explicit = {selection for selection in selections if selection is not None}
            if len(explicit) > 1:
                raise StoreError(400, "select one model per bot invocation")
            model = next(iter(explicit)) if explicit else json.loads(runtime["models_json"])[0]
            efforts = {match[3] for match in MENTION.finditer(message["content"]) if match[1] == bot["id"] and match[3]}
            if len(efforts) > 1:
                raise StoreError(400, "select one reasoning effort per bot invocation")
            return self.enqueue(message["author_id"], bot["organization_id"], message["topic_id"], f"bot/{bot['id']}/{model}",
                                client_id, through=message["sequence"],
                                prompt=message["content"], db=db, mention=True, effort=next(iter(efforts), None))

    def _decline(self, db, bot, requester, topic, sequence, client_id, model):
        """A terminal, visible refusal: the requester sees why, no CLI is invoked and no prompt is retained."""
        self.access(db, bot, topic)
        scope = db.execute("SELECT t.channel_id,c.project_id FROM topics t JOIN channels c ON c.id=t.channel_id "
                           "WHERE t.id=?", (topic,)).fetchone()
        run_id, now = _id(), int(time.time())
        db.execute("INSERT INTO bot_runs(id,organization_id,project_id,channel_id,topic_id,bot_id,requested_by,model,client_id,"
                   "request_json,prompt,status,phase,created_at,updated_at,context_through_sequence,error_code) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,'','cancelled','finishing',?,?,?,?)",
                   (run_id, bot["organization_id"], scope["project_id"], scope["channel_id"], topic, bot["id"],
                    requester, model, client_id, json.dumps({"declined": DECLINED}), now, now, sequence, DECLINED))
        self.store._audit(db, bot["organization_id"], requester, "bot.inference.decline", run_id)
        return self.public(db.execute("SELECT * FROM bot_runs WHERE id=?", (run_id,)).fetchone())

    @staticmethod
    def timing(row):
        """Server stages on the server clock; runner stages as reported. Unknown stays null, never derived from seconds."""
        value = dict(row)
        enqueued, claimed, committed = value.get("enqueued_at_ms"), value.get("claimed_at_ms"), value.get("committed_at_ms")
        runner = json.loads(value.get("runner_timing_json") or "null") or {}
        return {
            "enqueued_at_ms": enqueued, "claimed_at_ms": claimed, "committed_at_ms": committed,
            "queue_wait_ms": claimed - enqueued if enqueued is not None and claimed is not None else None,
            "claim_to_commit_ms": committed - claimed if claimed is not None and committed is not None else None,
            "runner": {stage: runner.get(stage) for stage in RUNNER_STAGES},
            "resolved_model": runner.get("resolved_model"),
        }

    @staticmethod
    def public(row):
        value = dict(row)
        direct = value.get("direct_conversation_id")
        # A run in a direct conversation has no topic. Runners name their workspace after these three IDs, so the
        # conversation's own ID stands in for each: one private workspace per conversation, and older runners keep working.
        scope = {key: direct if direct and value[key] is None else value[key] for key in ("project_id", "channel_id", "topic_id")}
        return {"bot_name": value.get("bot_name"), "timing": BotRuntime.timing(row),
                "effort": value.get("effort"), "gateway": json.loads(value.get("gateway_json", "{}")),
                "activity": json.loads(value.get("activity_json", "[]")),
                "owner_user_id": value.get("owner_user_id"), "approval_by": value.get("approval_by"),
                "direct_conversation_id": direct, **scope,
                **{key: row[key] for key in ("id", "organization_id", "bot_id",
                "requested_by", "model", "status", "phase", "created_at", "updated_at", "context_through_sequence",
                "output_text", "error_code")}}

    def listing(self, user, org, topic):
        with self.store.transaction(write=True) as db:
            self.store._topic_access(db, user, org, topic)
            self.expire(db, org)
            return [self.public(row) for row in db.execute("SELECT r.*,u.name AS bot_name,b.created_by AS owner_user_id,"
                + APPROVAL_BY + " FROM bot_runs r JOIN users u ON u.id=r.bot_id JOIN bots b ON b.id=r.bot_id "
                "WHERE r.organization_id=? AND r.topic_id=? ORDER BY r.created_at DESC,r.id DESC LIMIT 100", (org, topic))]

    def _viewer(self, db, user, org, run, *, write=False):
        """The caller's role where the run lives: its topic, or the direct conversation they take part in."""
        if run["direct_conversation_id"]:
            from .direct_messages import DirectMessages

            return DirectMessages.access(db, user, org, run["direct_conversation_id"], write=write)[0]
        return self.store._topic_access(db, user, org, run["topic_id"], write=write)[0]

    def get(self, user, org, run_id):
        with self.store.transaction(write=True) as db:
            run = _row(db.execute("SELECT * FROM bot_runs WHERE organization_id=? AND id=?", (org, run_id)).fetchone())
            self._viewer(db, user, org, run)
            self.expire(db, org)
            return self.public(db.execute("SELECT r.*,u.name AS bot_name,b.created_by AS owner_user_id," + APPROVAL_BY
                                          + " FROM bot_runs r JOIN users u ON u.id=r.bot_id JOIN bots b ON b.id=r.bot_id "
                                          "WHERE r.id=?", (run_id,)).fetchone())

    def stop(self, user, org, run_id):
        """Fence future bot effects, without claiming an external invocation stopped."""
        # This default write transaction is the shared SQLite/PG legacy-writer
        # fence used by claim, progress, consent consumption and completion.
        # Keep it until a replacement covers every effect and its source reread.
        with self.store.transaction(write=True) as db:
            run = _row(db.execute("SELECT * FROM bot_runs WHERE organization_id=? AND id=?", (org, run_id)).fetchone())
            role = self._viewer(db, user, org, run, write=True)
            owner = db.execute("SELECT created_by FROM bots WHERE id=?", (run["bot_id"],)).fetchone()[0]
            if user not in {run["requested_by"], owner} and role not in {"owner", "admin"}:
                raise StoreError(403, "only the requester, bot owner or administrator can stop this invocation")
            if run["status"] in ACTIVE:
                now = int(time.time())
                status = "cancelled" if run["status"] == "queued" or run["error_code"] == "approval_transport_ended" else "outcome_unknown"
                # The status fences every lease effect; the retained lease authenticates
                # only the runner's receipt that its CLI ended (see finish).
                db.execute(
                    "UPDATE bot_runs SET status=?,error_code='stop_requested',updated_at=? WHERE id=?",
                    (status, now, run_id),
                )
                db.execute(
                    "UPDATE bot_execution_approvals SET status='denied',decided_at=? "
                    "WHERE run_id=? AND status IN ('pending','approved')", (now, run_id),
                )
                db.execute("UPDATE bot_issue_approvals SET status='denied',decided_at=? "
                           "WHERE run_id=? AND status IN ('pending','approved')", (now, run_id))
                db.execute("UPDATE bot_compute_approvals SET status='denied' WHERE run_id=? AND status='pending'", (run_id,))
                self.store._audit(db, org, user, "bot.inference.stop", run_id)
            return self.public(db.execute("SELECT r.*,b.created_by AS owner_user_id FROM bot_runs r "
                                          "JOIN bots b ON b.id=r.bot_id WHERE r.id=?", (run_id,)).fetchone())

    @staticmethod
    def expire(db, org):
        now = int(time.time())
        db.execute("UPDATE bot_runs SET status='outcome_unknown',error_code='runner_lost',updated_at=? "
                   "WHERE organization_id=? AND status IN ('running','waiting_approval') AND updated_at<? "
                   "AND NOT (status='waiting_approval' AND " + AWAITING_CONSENT + ")", (now, org, now - 120))

    def claim(self, credential):
        self.bots._meter(credential, "read")
        with self.store.transaction(write=True) as db:
            bot = self.bots._authenticate(db, credential)
            now = int(time.time())
            db.execute("UPDATE bot_runs SET status='outcome_unknown',error_code='runner_lost',updated_at=? "
                       "WHERE bot_id=? AND status IN ('running','waiting_approval') AND updated_at<? "
                       "AND NOT (status='waiting_approval' AND " + AWAITING_CONSENT + ")", (now, bot["id"], now - 120))
            from .bot_delegation import dispatch as dispatch_delegates
            from .bot_goals import dispatch

            dispatch_delegates(self, db, bot)
            dispatch(self, db, bot)
            if db.execute("SELECT 1 FROM bot_runs WHERE bot_id=? AND status IN ('running','waiting_approval')", (bot["id"],)).fetchone():
                return None
            for row in db.execute("SELECT * FROM bot_runs WHERE bot_id=? AND status='queued' ORDER BY created_at,id", (bot["id"],)):
                try:
                    self._run_access(db, bot, row)
                except StoreError:
                    db.execute("UPDATE bot_runs SET status='cancelled',error_code='access_changed' WHERE id=?", (row["id"],))
                    continue
                lease = token()
                db.execute("UPDATE bot_runs SET status='running',lease_hash=?,updated_at=?,claimed_at_ms=? WHERE id=?",
                           (digest(lease), now, int(time.time() * 1000), row["id"]))
                from .research_profiles import topic_instructions

                if row["direct_conversation_id"]:
                    instructions, history = {}, self._direct_history(db, row)
                else:
                    kind = db.execute("SELECT kind FROM topics WHERE id=?", (row["topic_id"],)).fetchone()[0]
                    instructions = topic_instructions(db, row["organization_id"], row["topic_id"]) if kind == "beta" else {}
                    from .bot_context import conversation

                    history = conversation(self, db, bot, row)
                return {**self.public(db.execute("SELECT * FROM bot_runs WHERE id=?", (row["id"],)).fetchone()),
                        "lease": lease, "prompt": row["prompt"], "system_prompt": instructions.get("system_prompt", ""),
                        "context": history}
            return None

    def _run_access(self, db, bot, run):
        if run["direct_conversation_id"]:
            self._direct_access(db, bot, run)
            runtime = _row(db.execute("SELECT * FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone())
            if run["model"] not in json.loads(runtime["models_json"]):
                raise StoreError(403, "bot model is no longer advertised")
            return
        self.access(db, bot, run["topic_id"], write=True)
        self.store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"], write=True)
        delegated_goal = db.execute("SELECT g.status FROM bot_agent_tasks t JOIN agent_goals g ON g.id=t.goal_id "
                                    "WHERE t.child_run=?", (run["id"],)).fetchone()
        if delegated_goal and delegated_goal[0] != "active":
            raise StoreError(409, "the delegated goal is no longer active")
        stopped_parent = db.execute(
            "WITH RECURSIVE edges(child,parent) AS (SELECT child_run,parent_run FROM bot_agent_tasks "
            "UNION SELECT next_run,parent_run FROM bot_agent_handoffs), "
            "parents(id) AS (SELECT parent FROM edges WHERE child=? "
            "UNION SELECT e.parent FROM edges e JOIN parents p ON e.child=p.id) "
            "SELECT 1 FROM parents p JOIN bot_runs r ON r.id=p.id "
            "WHERE r.status IN ('cancelled','failed','outcome_unknown') LIMIT 1", (run["id"],),
        ).fetchone()
        if stopped_parent:
            raise StoreError(409, "the delegating invocation was stopped or has an unknown outcome")
        require(db, bot, run["requested_by"])
        runtime = _row(db.execute("SELECT * FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone())
        if run["model"] not in json.loads(runtime["models_json"]):
            raise StoreError(403, "bot model is no longer advertised")

    def leased(self, db, credential, run_id, lease, *, terminal=False):
        bot = self.bots._authenticate(db, credential)
        run = _row(db.execute("SELECT * FROM bot_runs WHERE id=? AND bot_id=?", (run_id, bot["id"])).fetchone())
        self._run_access(db, bot, run)
        if run["error_code"] == "approval_transport_ended" and not terminal:
            raise StoreError(409, "bot transport ended before execution; review and stop the retained request")
        if not isinstance(lease, str) or digest(lease) != run["lease_hash"] or run["status"] not in (
                ACTIVE | TERMINAL if terminal else ACTIVE - {"queued"}):
            raise StoreError(409, "bot invocation lease is no longer active; do not retry execution")
        waiting = run["status"] == "waiting_approval" and db.execute(
            "SELECT 1 FROM bot_runs WHERE id=? AND " + AWAITING_CONSENT, (run_id,)).fetchone()
        if run["status"] in ACTIVE and run["updated_at"] < int(time.time()) - 120 and not waiting:
            raise StoreError(409, "bot invocation heartbeat expired; reconcile its outcome")
        return bot, run

    def progress(self, credential, run_id, lease, phase=None, activity=None):
        from .bot_activity import LABELS

        if phase is not None and phase not in PHASES or activity is not None and activity not in LABELS:
            raise StoreError(400, "unsupported bot activity phase")
        self.bots._meter(credential, "read")
        with self.store.transaction(write=True) as db:
            _, run = self.leased(db, credential, run_id, lease)
            task = db.execute("SELECT a.status FROM topic_agent_tasks t JOIN topic_agent_threads a ON a.root_run=t.root_run "
                              "WHERE t.child_bot_run=?", (run_id,)).fetchone()
            if task and task[0] != "active":
                raise StoreError(409, "topic agents are paused or stopped; retain this local invocation's outcome")
            entries = json.loads(run["activity_json"])
            now = int(time.time())
            if activity and (not entries or entries[-1]["kind"] != activity) and (not entries or now - entries[-1]["at"] >= 1):
                entries = (entries + [{"kind": activity, "label": LABELS[activity], "at": now}])[-12:]
            db.execute("UPDATE bot_runs SET phase=?,updated_at=?,activity_json=? WHERE id=?",
                       (phase or run["phase"], now, json.dumps(entries), run_id))
            waiting = run["status"] == "waiting_approval" and bool(db.execute(
                "SELECT 1 FROM bot_runs WHERE id=? AND " + AWAITING_CONSENT, (run_id,)).fetchone())
            return {"active": True, "phase": phase or run["phase"], "waiting_for_approval": waiting}

    def finish(self, credential, run_id, lease, status, output="", error=None, timing=None):
        """``timing`` is the runner's stage report; a replayed receipt keeps the first one."""
        if status not in TERMINAL or not isinstance(output, str) or len(output.encode()) > 128 * 1024:
            raise StoreError(400, "invalid bounded bot completion")
        timing = runner_timing(timing)
        if status == "succeeded" and not output.strip() or error is not None and error not in {
                "cli_failed", "runner_lost", "cancelled", "gateway_unavailable", "gateway_not_attached"}:
            raise StoreError(400, "invalid bot completion receipt")
        self.bots._meter(credential, "write")
        with self.store.transaction(write=True) as db:
            bot, run = self.leased(db, credential, run_id, lease, terminal=True)
            if run["error_code"] in STOPPED and run["status"] in {"outcome_unknown", "cancelled"}:
                # Any receipt after a stop means the runner's CLI has ended. The stop
                # fenced all lease effects, so only consumed approvals keep it unknown.
                # The receipt's own output is discarded.
                if run["status"] == "outcome_unknown" and not self._consumed_effects(db, run_id):
                    db.execute("UPDATE bot_runs SET status='cancelled',phase='finishing',updated_at=?,"
                               "runner_timing_json=COALESCE(runner_timing_json,?) WHERE id=?",
                               (int(time.time()), json.dumps(timing) if timing else None, run_id))
                return self.public(db.execute("SELECT * FROM bot_runs WHERE id=?", (run_id,)).fetchone())
            if run["status"] in TERMINAL:
                if run["status"] != status or run["output_text"] != output or run["error_code"] != error:
                    raise StoreError(409, "bot completion receipt differs from the retained outcome")
                return self.public(run)
            if (status in {"failed", "outcome_unknown"} and error == "cli_failed"
                    and run["status"] == "waiting_approval" and db.execute(
                        "SELECT 1 FROM bot_runs WHERE id=? AND " + AWAITING_CONSENT, (run_id,)).fetchone()
                    and not self._consumed_effects(db, run_id)):
                # A stopped MCP transport is not evidence that its unconsumed
                # action ran. Retain exact consent, but never promise delivery.
                db.execute("UPDATE bot_runs SET error_code='approval_transport_ended',updated_at=? WHERE id=?",
                           (int(time.time()), run_id))
                return self.public(db.execute("SELECT * FROM bot_runs WHERE id=?", (run_id,)).fetchone())
            committed = None
            if status == "succeeded":
                for index, start in enumerate(range(0, len(output), 16000)):
                    if run["direct_conversation_id"]:
                        db.execute("INSERT INTO direct_messages(id,organization_id,conversation_id,author_id,content,client_id,created_at) "
                                   "VALUES(?,?,?,?,?,?,?)", (_id(), run["organization_id"], run["direct_conversation_id"], bot["id"],
                                                             output[start:start + 16000], f"bot-run-{run_id}-{index}", int(time.time())))
                    else:
                        self.store._post_message(db, bot["id"], run["organization_id"], run["topic_id"], output[start:start + 16000],
                                                 f"bot-run-{run_id}-{index}", sender_kind="bot")
                committed = int(time.time() * 1000)
            db.execute("UPDATE bot_runs SET status=?,phase='finishing',output_text=?,error_code=?,updated_at=?,"
                       "committed_at_ms=?,runner_timing_json=? WHERE id=?",
                       (status, output, error, int(time.time()), committed, json.dumps(timing) if timing else None, run_id))
            return self.public(db.execute("SELECT * FROM bot_runs WHERE id=?", (run_id,)).fetchone())

    @staticmethod
    def _consumed_effects(db, run_id):
        return bool(db.execute("SELECT 1 FROM bot_execution_approvals WHERE run_id=? AND status='consumed' "
            "UNION ALL SELECT 1 FROM bot_issue_approvals WHERE run_id=? AND status IN ('consumed','created','outcome_unknown') "
            "UNION ALL SELECT 1 FROM bot_compute_approvals WHERE run_id=? AND status='submitted' LIMIT 1",
            (run_id, run_id, run_id)).fetchone())

    def receipt(self, credential, run_id):
        """Read only a bot's own lifecycle; this never restores a tool/run lease."""
        self.bots._meter(credential, "read")
        with self.store.transaction() as db:
            bot = self.bots._authenticate(db, credential)
            run = _row(db.execute("SELECT status,error_code FROM bot_runs WHERE id=? AND bot_id=?",
                                  (run_id, bot["id"])).fetchone())
            return {"status": run["status"], "error_code": run["error_code"]}

    def request_execution(self, credential, run_id, lease, command):
        if not isinstance(command, list) or not 1 <= len(command) <= 32 or any(
                not isinstance(part, str) or not part or len(part) > 8000 or "\0" in part for part in command):
            raise StoreError(400, "execution requires a bounded exact argument vector")
        self.bots._meter(credential, "write")
        document = json.dumps(command, ensure_ascii=True, separators=(",", ":"))
        with self.store.transaction(write=True) as db:
            bot, run = self.leased(db, credential, run_id, lease)
            runtime = db.execute("SELECT * FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone()
            if command[0] == "reasoning-dev-test":
                self._developer_test_access(db, bot, run, command)
            if not runtime["local_tools"]:
                raise StoreError(403, "the bot owner disabled local execution")
            previous = db.execute("SELECT * FROM bot_execution_approvals WHERE run_id=? AND command_sha256=?",
                                  (run_id, digest(document))).fetchone()
            if previous:
                return dict(previous)
            identity, now = _id(), int(time.time())
            db.execute("INSERT INTO bot_execution_approvals VALUES(?,?,?,?,?,?,'pending',?,NULL,NULL)",
                       (identity, run_id, bot["id"], bot["created_by"], document, digest(document), now))
            db.execute("UPDATE bot_runs SET status='waiting_approval',phase='waiting_approval',updated_at=? WHERE id=?", (now, run_id))
            return dict(db.execute("SELECT * FROM bot_execution_approvals WHERE id=?", (identity,)).fetchone())

    def approvals(self, user, org):
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            records = []
            for row in db.execute("SELECT a.*,r.topic_id,r.direct_conversation_id,r.requested_by,u.name AS requester_name,"
                "t.name AS topic_name FROM bot_execution_approvals a JOIN bot_runs r ON r.id=a.run_id JOIN users u ON u.id=r.requested_by "
                "LEFT JOIN topics t ON t.id=r.topic_id WHERE a.owner_id=? AND r.organization_id=? "
                "AND (a.status='pending' OR (a.status='approved' AND r.error_code='approval_transport_ended')) "
                "AND r.status='waiting_approval' ORDER BY a.created_at LIMIT 100", (user, org)):
                try:
                    run = db.execute("SELECT * FROM bot_runs WHERE id=?", (row["run_id"],)).fetchone()
                    bot = db.execute("SELECT * FROM bots WHERE id=?", (row["bot_id"],)).fetchone()
                    self._run_access(db, bot, run)
                    self._execution_access(db, bot, run, row)
                except StoreError:
                    continue
                records.append({**dict(row), "delivery_interrupted": run["error_code"] == "approval_transport_ended"})
            return records

    def approve(self, user, org, approval_id, allow, *, request_sha256=None):
        if type(allow) is not bool:
            raise StoreError(400, "choose an explicit execution approval decision")
        with self.store.transaction(write=True) as db:
            approval = _row(db.execute("SELECT a.* FROM bot_execution_approvals a JOIN bot_runs r ON r.id=a.run_id "
                "WHERE a.id=? AND a.owner_id=? AND r.organization_id=?", (approval_id, user, org)).fetchone())
            run = db.execute("SELECT * FROM bot_runs WHERE id=?", (approval["run_id"],)).fetchone()
            bot = db.execute("SELECT * FROM bots WHERE id=?", (approval["bot_id"],)).fetchone()
            self._run_access(db, bot, run)
            self._execution_access(db, bot, run, approval, request_sha256=request_sha256)
            if allow and run["error_code"] == "approval_transport_ended":
                raise StoreError(409, "bot transport ended before execution; stop this invocation and submit a new request")
            withdraw = not allow and approval["status"] == "approved" and run["error_code"] == "approval_transport_ended"
            if (approval["status"] != "pending" and not withdraw) or run["status"] != "waiting_approval":
                raise StoreError(409, "execution request is no longer awaiting consent")
            db.execute("UPDATE bot_execution_approvals SET status=?,decided_at=? WHERE id=?",
                       ("approved" if allow else "denied", int(time.time()), approval_id))
            self.store._audit(db, org, user, "bot.execution.approve" if allow else "bot.execution.deny", approval_id)
            return {"status": "approved" if allow else "denied"}

    def execution(self, credential, run_id, lease, approval_id, *, consume=False):
        with self.store.transaction(write=consume) as db:
            bot, run = self.leased(db, credential, run_id, lease)
            row = _row(db.execute("SELECT * FROM bot_execution_approvals WHERE id=? AND run_id=?", (approval_id, run_id)).fetchone())
            if consume:
                if run["error_code"] == "approval_transport_ended":
                    raise StoreError(409, "bot transport ended before execution; this consent cannot be consumed")
                self._execution_access(db, bot, run, row)
                if row["status"] != "approved":
                    raise StoreError(409, "this exact execution has not been approved or was already consumed")
                db.execute("UPDATE bot_execution_approvals SET status='consumed',consumed_at=? WHERE id=?", (int(time.time()), approval_id))
                db.execute("UPDATE bot_runs SET status='running',phase='using_tools',updated_at=? WHERE id=?", (int(time.time()), run_id))
            return {**dict(row), "status": "consumed" if consume else row["status"]}

    def _execution_access(self, db, bot, run, approval, *, request_sha256=None):
        if (approval["bot_id"] != bot["id"] or approval["owner_id"] != bot["created_by"]
                or approval["run_id"] != run["id"] or digest(approval["command_json"]) != approval["command_sha256"]
                or request_sha256 is not None and request_sha256 != approval["command_sha256"]):
            raise StoreError(409, "execution request changed; review its exact action again")
        command = json.loads(approval["command_json"])
        if command[0] == "reasoning-dev-test":
            self._developer_test_access(db, bot, run, command)
        runtime = db.execute("SELECT local_tools FROM bot_runtimes WHERE bot_id=?", (bot["id"],)).fetchone()
        if not runtime or not runtime[0]:
            raise StoreError(403, "the bot owner disabled local execution")

    @staticmethod
    def _developer_test_access(db, bot, run, command):
        from .bot_developer import BotDeveloper

        grant = BotDeveloper.permission(db, bot, run)
        if (len(command) != 5 or command[1] not in json.loads(grant["repositories_json"])
                or command[2] not in {"smoke", "bots", "knowledge", "frontend"}
                or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", command[3])
                or not re.fullmatch(r"[a-f0-9]{64}", command[4])):
            raise StoreError(403, "developer test profile or repository is not currently permitted")
