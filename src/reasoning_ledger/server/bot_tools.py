"""Authenticated narrow local-agent tools backed by the canonical research store.

Copyright Benjamin Antieau and Deven Manam. SPDX-License-Identifier: Apache-2.0
"""

import json
import time

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..service import RevisionObservations
from .bot_conversations import BotConversations, discovery_tool, topic_metadata
from .knowledge_store import KnowledgeStore
from .store import StoreError
from .workbench_tools import DESCRIPTIONS, OPERATIONS, READS, MathematicalLedger, operation_schema

DEVELOPER_NOT_PERMITTED = (
    "This bot's developer tools (GitHub issues, server diagnostics, development tests) are not available to the person "
    "who requested this invocation. If they asked for one of these actions, tell them that the bot owner must add them "
    "under My bots -> Developer tools -> permitted requesters; do not attempt the action another way.")


def argument_problem(error):
    """Describe a schema violation from schema-defined facts only; argument values are never echoed."""
    location = "/".join(str(part)[:60] for part in error.absolute_path)[:200] or "arguments"
    value = error.validator_value
    if error.validator == "required" and isinstance(error.instance, dict):
        reason = "missing required " + ", ".join(str(name) for name in value if name not in error.instance)
    elif error.validator == "additionalProperties":
        allowed = sorted(error.schema.get("properties", {}))
        reason = "has unsupported fields; allowed: " + ", ".join(allowed) if allowed else "accepts no fields"
    elif error.validator in {"type", "enum", "const", "minLength", "maxLength", "minimum", "maximum",
                             "minItems", "maxItems", "pattern"}:
        reason = f"violates {error.validator} {json.dumps(value)[:300]}"
    else:
        reason = f"violates {error.validator}"
    return f"{location} {reason}"


class BotTools:
    def __init__(self, runtime, *, compute=None):
        self.runtime, self.store = runtime, runtime.store
        self.compute = compute

    def context(self, credential, run_id, lease, *, meter=True):
        if meter:
            self.runtime.bots._meter(credential, "read")
        with self.store.transaction() as db:
            bot, run = self.runtime.leased(db, credential, run_id, lease)
            if run["direct_conversation_id"]:
                # Your conversation with your bot has no topic, so no Reasoning research tools: the bot answers with its own
                # local abilities, and local execution still asks you first. The scope names the conversation, as in claim.
                direct = run["direct_conversation_id"]
                return {"tools": [], "scope": {"organization_id": run["organization_id"], "project_id": direct, "channel_id": direct,
                        "topic_id": direct, "project_name": "Direct messages", "channel_name": "Direct messages",
                        "topic_title": "Direct messages", "topic_kind": "direct"}, "bot_id": bot["id"], "owner_id": bot["created_by"],
                        "requested_by": run["requested_by"], "developer": {"enabled": False}, "gateway": json.loads(run["gateway_json"])}
            delegation = db.execute("SELECT a.status FROM topic_agent_tasks t JOIN topic_agent_threads a ON a.root_run=t.root_run "
                                    "WHERE t.child_bot_run=?", (run_id,)).fetchone()
            if delegation and delegation[0] != "active":
                raise StoreError(409, "topic agents are paused or stopped")
            topic = self.store._topic_access(db, bot["created_by"], run["organization_id"], run["topic_id"])[4]
            collections = set(json.loads(topic["knowledge_policy_json"]))
            scopes = set(json.loads(bot["scopes_json"]))
            tools = []
            from .research_media import MEDIA_READ_TOOLS, media_tools
            from .research_tools import schema

            tools.append({"name": "topic_context", "description": "Verify the scoped Reasoning gateway and retrieve "
                          "recent authorized conversation and the exact tool names granted to this invocation.",
                          "input_schema": schema({})})
            from .bot_research import BotResearch

            research = BotResearch(self.runtime, credential, run_id, lease, compute=self.compute)
            tools.extend({"name": item.name, "description": item.description, "input_schema": item.input_schema}
                         for item in research.registry.list())
            if self.compute and "compute" in research.enabled and "knowledge:write" in scopes and topic["kind"] != "alpha":
                from .noether_compute import definitions

                tools.extend({key: value for key, value in definition.items() if key != "risk"} for definition in definitions())
                tools.append({"name": "compute_submit", "description": "Request the bot owner's approval for exact "
                              "source, immutable image and resource limits. Approved jobs use the same isolated worker "
                              "and artifact contract on DigitalOcean, owned hardware or Modal; no Docker socket is exposed.",
                              "input_schema": schema({"image_id": {"type": "string", "maxLength": 64},
                                  "language": {"enum": ["python", "sage", "gap", "pari", "julia", "lean"]},
                                  "program": {"type": "string", "minLength": 1, "maxLength": 16000},
                                  "resources": {"type": "object"}}, ["image_id", "language", "program", "resources"])})
            tools.extend([
                {"name": "tool_search", "description": "Discover granted Reasoning tools and their exact schemas.",
                 "input_schema": schema({"query": {"type": "string", "minLength": 1, "maxLength": 200}}, ["query"])},
                {"name": "tool_call", "description": "Invoke a granted tool using its exact discovered schema.",
                 "input_schema": schema({"name": {"type": "string", "maxLength": 100}, "arguments": {"type": "object"}},
                                        ["name", "arguments"])},
            ])
            if db.execute("SELECT 1 FROM agent_goals WHERE bot_run_id=? AND status='active'", (run_id,)).fetchone():
                tools.append({"name": "goal_checkpoint", "description": "Record goal progress and evidence. Complete "
                              "only when every acceptance criterion has been met.", "input_schema": schema({
                                  "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                                  "status": {"enum": ["active", "complete", "blocked"]}}, ["summary"])})

            for name, description, properties, required, _ in media_tools():
                needed = "knowledge:read" if name in MEDIA_READ_TOOLS else "knowledge:write"
                if needed in scopes:
                    tools.append({"name": name, "description": description, "input_schema": schema(properties, required)})
            if "knowledge:read" in scopes:
                tools.extend([
                    {"name": "artifact_list", "description": "List project text files; retrieve only relevant exact revisions.",
                     "input_schema": schema({})},
                    {"name": "research_file_read", "description": "Read a bounded exact project text-file revision, "
                     "including saved .tex sources.",
                     "input_schema": schema({"path": {"type": "string", "maxLength": 200},
                                             "revision": {"type": "integer", "minimum": 1},
                                             "offset": {"type": "integer", "minimum": 0},
                                             "length": {"type": "integer", "minimum": 1, "maximum": 12000}}, ["path"])},
                ])
            if "knowledge:write" in scopes:
                tools.append({"name": "artifact_write", "description": "Save a persistent project text file and return its "
                              "clickable revision link. Overwrites require the current SHA-256.",
                              "input_schema": schema({"path": {"type": "string", "maxLength": 200},
                                  "text": {"type": "string", "maxLength": 64000},
                                  "expected_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}, ["path", "text"])})
            tools.append({"name": "topic_history", "description": "Read a small page of this topic's earlier conversation on demand.",
                "input_schema": {"type": "object", "properties": {"before": {"type": "integer", "minimum": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "additionalProperties": False}})
            tools.append({"name": "messages_search", "description": "Search a small page of conversations accessible to both "
                          "this bot and the person requesting this run, with source topic titles and project/channel names. "
                          "Use topics_search first to find a topic by name. Fetch only relevant context, "
                          "never entire organization history.",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "topic_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, "required": ["query"], "additionalProperties": False}})
            tools.append(discovery_tool())
            if topic["kind"] == "beta" and "knowledge" in research.enabled:
                from .account_tools import DESCRIPTIONS as ACCOUNT_DESCRIPTIONS
                from .account_tools import schemas

                for name, definition in schemas().items():
                    if name == "account_request_review":
                        continue
                    needed = "knowledge:write" if name == "account_update" else "knowledge:read"
                    if needed in scopes:
                        tools.append({"name": name, "description": ACCOUNT_DESCRIPTIONS[name], "input_schema": definition})
            for name, function in OPERATIONS.items():
                collection = "bibliography" if "reference" in name else "ledger"
                if collection not in collections or ("knowledge:read" if name in READS else "knowledge:write") not in scopes:
                    continue
                definition = operation_schema(function)
                if name in READS:
                    definition["properties"]["scope"] = {"enum": ["working_set", "topic", "project"]}
                tools.append({"name": name, "description": DESCRIPTIONS[name], "input_schema": definition})
            if "ledger" in collections and "knowledge:read" in scopes:
                tools.append({"name": "ledger_read", "description": "Read an exact accessible mathematical record and revision.",
                              "input_schema": {"type": "object", "properties": {"ledger_id": {"type": "string", "maxLength": 100},
                                                 "history": {"type": "boolean"}}, "required": ["ledger_id"],
                                                 "additionalProperties": False}})
            if "journal" in collections and "knowledge" in research.enabled:
                text = {"type": "string", "minLength": 1, "maxLength": 16000}
                entry = {"type": "string", "pattern": "^[a-f0-9]{32}$"}
                if "knowledge:read" in scopes:
                    tools.append({"name": "blog_read", "description": "Read an exact project blog revision, "
                                  "including drafts and published posts.",
                        "input_schema": {"type": "object", "properties": {"entry_id": entry,
                            "revision": {"type": "integer", "minimum": 1}}, "required": ["entry_id"],
                                         "additionalProperties": False}})
                    tools.append({"name": "blog_search", "description": "Search accessible project blog drafts and published posts.",
                                  "input_schema": {"type": "object", "properties": {"query": {"type": "string", "maxLength": 200},
                                      "scope": {"enum": ["working_set", "topic", "project"]},
                                      "before": {"type": "string", "maxLength": 100}},
                                                   "additionalProperties": False}})
                if "knowledge:write" in scopes:
                    for name in ("blog_create", "blog_revise"):
                        props = {"title": {"type": "string", "minLength": 1, "maxLength": 300}, "body": text,
                                 "state": {"enum": ["draft", "published"]}}
                        required = ["title", "body"]
                        if name == "blog_revise":
                            props.update(entry_id=entry, expected_revision={"type": "integer", "minimum": 1})
                            required += ["entry_id", "expected_revision"]
                        tools.append({"name": name, "description": "Save a project blog draft or publish authorized writing. "
                                      "Existing published posts can be edited; revisions are guarded and the audience stays private.",
                            "input_schema": {"type": "object", "properties": props, "required": required,
                                             "additionalProperties": False}})
                    tools.append({"name": "blog_publish", "description": "Publish the exact current project blog "
                                  "revision when authorized by the user.",
                                  "input_schema": {"type": "object", "properties": {"entry_id": entry,
                                      "expected_revision": {"type": "integer", "minimum": 1}},
                                      "required": ["entry_id", "expected_revision"], "additionalProperties": False}})
            if delegation:
                tools = [tool for tool in tools if tool["name"] not in {"topic_history", "messages_search", "topics_search"}]
            if db.execute("SELECT 1 FROM bot_agent_tasks WHERE child_run=?", (run_id,)).fetchone():
                tools = [tool for tool in tools if tool["name"] not in {
                    "topic_history", "messages_search", "topics_search", "goal_checkpoint"}]
            if "knowledge:write" in scopes:
                tools.append({"name": "agent_delegate", "description": "Delegate a narrow task to a bot worker, librarian, "
                              "critic or formalizer. Supply only needed context. End this turn; integration resumes automatically.",
                              "input_schema": schema({"role": {"enum": ["worker", "critic", "librarian", "formalizer", "verifier"]},
                                  "objective": {"type": "string", "minLength": 1, "maxLength": 8000},
                                  "context": {"type": "string", "maxLength": 24000},
                                  "bot_model": {"type": "string", "maxLength": 150}}, ["role", "objective"])})
                tools.extend([
                    {"name": "agent_tasks", "description": "List this coordinator's own delegated tasks; do not busy-poll.",
                     "input_schema": schema({})},
                    {"name": "agent_read", "description": "Read one exact result from this coordinator's assigned workers.",
                     "input_schema": schema({"task_id": {"type": "string", "pattern": "^[a-f0-9]{32}$"}}, ["task_id"])},
                ])
            from .bot_developer import BotDeveloper

            try:
                developer = BotDeveloper.permission(db, bot, run)
                developer_policy = {"enabled": True, "repositories": json.loads(developer["repositories_json"])}
            except StoreError:
                developer_policy = {"enabled": False}
                grant = db.execute("SELECT enabled FROM bot_developer_permissions WHERE bot_id=?", (bot["id"],)).fetchone()
                if grant and grant[0]:
                    # Presence already marks developer bots; only this requester's exclusion is stated.
                    developer_policy["reason"] = "requester_not_permitted"
            return {"tools": tools, "scope": topic_metadata(self.store, db, run["requested_by"],
                    run["organization_id"], run["topic_id"]), "bot_id": bot["id"],
                    "owner_id": bot["created_by"], "requested_by": run["requested_by"], "developer": developer_policy,
                    "gateway": json.loads(run["gateway_json"])}

    def invoke(self, credential, run_id, lease, name, arguments, client_id):
        from .bot_activity import tool_activity

        context = self.context(credential, run_id, lease, meter=name not in {"media_chunk", "media_download_chunk"})
        definition = next((tool for tool in context["tools"] if tool["name"] == name), None)
        if definition is None:
            raise StoreError(403, "this bot invocation does not grant that mathematical tool")
        if name not in {"media_chunk", "media_download_chunk"}:
            self.runtime.progress(credential, run_id, lease, "using_tools", tool_activity(name))
        readonly = READS | {"blog_read", "blog_search", "topic_history", "messages_search", "topics_search", "media_list", "media_read",
                            "media_download_chunk",
                            "artifact_list", "research_file_read", "account_read", "noether_operations", "compute_log_read"}
        if name in {"media_chunk", "media_download_chunk"}:
            from .security import digest

            now, quota = int(time.time()), "bot-media:" + digest(credential)
            with self.store.transaction(write=True) as db:
                self.runtime.leased(db, credential, run_id, lease)
                if self.store._postgres:
                    self.store._postgres.lock(db.native, quota)
                db.execute("DELETE FROM auth_attempts WHERE scope=? AND attempted_at<?", (quota, now - 60))
                count = db.execute("SELECT COUNT(*) FROM auth_attempts WHERE scope=?", (quota,)).fetchone()[0]
                if count >= 512:
                    raise StoreError(429, "bot attachment chunk rate limit reached")
                db.execute("INSERT INTO auth_attempts VALUES(?,?)", (quota, now))
        elif name not in readonly:
            self.runtime.bots._meter(credential, "write")
        try:
            Draft202012Validator(definition["input_schema"]).validate(arguments)
        except ValidationError as exc:
            raise StoreError(400, f"invalid mathematical tool arguments for {name}: {argument_problem(exc)}") from None
        scope = context["scope"]
        if name not in {"tool_search", "tool_call", "topic_context"}:
            with self.store.transaction(write=True) as db:
                self.runtime.leased(db, credential, run_id, lease)
                goal = db.execute("SELECT g.* FROM agent_goals g WHERE g.bot_run_id=? OR EXISTS "
                    "(SELECT 1 FROM bot_agent_tasks t WHERE t.child_run=? AND t.goal_id=g.id)", (run_id, run_id)).fetchone()
                if goal:
                    if (goal["status"] != "active" or goal["deadline_at"] <= int(time.time())
                            or goal["tool_calls"] >= goal["max_tool_calls"]):
                        raise StoreError(409, "goal is no longer active or its explicit tool/time limit was reached")
                    db.execute("UPDATE agent_goals SET tool_calls=tool_calls+1 WHERE id=?", (goal["id"],))
        if name in {"agent_delegate", "agent_tasks", "agent_read"}:
            from .bot_delegation import delegate, tasks

            if name == "agent_delegate":
                return delegate(self.runtime, credential, run_id, lease, **arguments)
            return tasks(self.runtime, credential, run_id, lease, **arguments)
        from .noether_compute import JOB_TOOLS, submission

        if name in {"compute_submit", *JOB_TOOLS}:
            from .bot_compute import BotCompute

            parameters = arguments if name == "compute_submit" else submission(name, arguments)
            result = BotCompute(self.runtime, self.compute).request(credential, run_id, lease, **parameters)
            with self.store.transaction(write=True) as db:
                self.runtime.leased(db, credential, run_id, lease)
                db.execute("INSERT INTO bot_tool_receipts(run_id,name,result_json,created_at) VALUES(?,?,?,?)",
                           (run_id, name, json.dumps(result), int(time.time())))
            return result
        if name == "goal_checkpoint":
            from .bot_goals import checkpoint

            return checkpoint(self.runtime, credential, run_id, lease, **arguments)
        if name == "tool_search":
            words = arguments["query"].lower().split()
            return [tool for tool in context["tools"] if tool["name"] not in {"tool_call", "tool_search"}
                    and any(word in (tool["name"] + " " + tool["description"]).lower() for word in words)][:8]
        if name == "tool_call":
            if arguments["name"] in {"tool_call", "tool_search"}:
                raise StoreError(400, "recursive tool discovery is not permitted")
            return {"name": arguments["name"], "result": self.invoke(credential, run_id, lease, arguments["name"],
                                                                        arguments["arguments"], client_id)}
        from .bot_research import BotResearch

        research = BotResearch(self.runtime, credential, run_id, lease, compute=self.compute)
        if name in {tool.name for tool in research.registry.list()}:
            from ..agents.scientific import ScientificToolError
            from .tool_failures import tool_failure

            try:
                result = research.registry.invoke(name, arguments)
            except (ValueError, ScientificToolError, OSError, TimeoutError) as exc:
                code, message = tool_failure(exc, name)
                result = {"error": message, "code": code}
            with self.store.transaction(write=True) as db:
                self.runtime.leased(db, credential, run_id, lease)
                db.execute("INSERT INTO bot_tool_receipts(run_id,name,result_json,created_at) VALUES(?,?,?,?)",
                           (run_id, name, json.dumps(result), int(time.time())))
            return result
        if name == "topic_context":
            from .bot_context import conversation

            with self.store.transaction(write=True) as db:
                bot, run = self.runtime.leased(db, credential, run_id, lease)
                result = {"ready": True, "scope": scope, "context": conversation(self.runtime, db, bot, run),
                          "tools": [tool["name"] for tool in context["tools"]]}
                if context["developer"].get("reason") == "requester_not_permitted":
                    result["notices"] = [DEVELOPER_NOT_PERMITTED]
                db.execute("UPDATE bot_runs SET gateway_json=? WHERE id=?",
                           (json.dumps({"ready": True, "stage": "cli", "tool_count": len(context["tools"])}), run_id))
                db.execute("INSERT INTO bot_tool_receipts(run_id,name,result_json,created_at) VALUES(?,?,?,?)",
                           (run_id, name, json.dumps(result), int(time.time())))
                return result
        if name in {"account_read", "account_update"}:
            from .account_tools import read_account
            from .research_accounts import ResearchAccounts

            def authorize_account(db):
                bot, run = self.runtime.leased(db, credential, run_id, lease)
                needed = "knowledge:write" if name == "account_update" else "knowledge:read"
                if needed not in json.loads(bot["scopes_json"]):
                    raise StoreError(403, "bot account permission changed")
                settings = db.execute("SELECT tools_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?",
                                      (run["organization_id"], run["topic_id"])).fetchone()
                if settings and "knowledge" not in json.loads(settings[0]):
                    raise StoreError(403, "knowledge tools are disabled")

            def source_access(db, topic):
                bot, run = self.runtime.leased(db, credential, run_id, lease)
                self.runtime.access(db, bot, topic)
                self.store._topic_access(db, run["requested_by"], run["organization_id"], topic)

            accounts = ResearchAccounts(self.store, authorize=authorize_account, topic_authorize=source_access)
            if name == "account_read":
                result = read_account(accounts, context["owner_id"], scope["organization_id"], scope["topic_id"], **arguments)
            else:
                result = accounts.update(context["owner_id"], scope["organization_id"], scope["topic_id"], **arguments,
                                         client_id="bot-" + run_id + "-" + client_id, actor_id=context["bot_id"])
            with self.store.transaction(write=True) as db:
                self.runtime.leased(db, credential, run_id, lease)
                db.execute("INSERT INTO bot_tool_receipts(run_id,name,result_json,created_at) VALUES(?,?,?,?)",
                           (run_id, name, json.dumps(result), int(time.time())))
            return result
        if name in {"artifact_list", "research_file_read", "artifact_write"}:
            from .research_artifacts import ResearchArtifacts

            def authorize_file(db):
                bot, _ = self.runtime.leased(db, credential, run_id, lease)
                needed = "knowledge:write" if name == "artifact_write" else "knowledge:read"
                if needed not in json.loads(bot["scopes_json"]):
                    raise StoreError(403, "this bot no longer grants the required file access")

            artifacts = ResearchArtifacts(self.store, authorize=authorize_file)
            method = {"artifact_list": artifacts.list, "research_file_read": artifacts.read, "artifact_write": artifacts.write}[name]
            if name != "artifact_write":
                return method(context["owner_id"], scope["organization_id"], scope["topic_id"], **arguments)
            result = method(context["owner_id"], scope["organization_id"], scope["topic_id"],
                            **{**arguments, "actor_id": context["bot_id"]})
            # The receipt lets the reply list the file it saved.
            with self.store.transaction(write=True) as db:
                self.runtime.leased(db, credential, run_id, lease)
                db.execute("INSERT INTO bot_tool_receipts(run_id,name,result_json,created_at) VALUES(?,?,?,?)",
                           (run_id, name, json.dumps(result), int(time.time())))
            return result
        if name.startswith("media_"):
            from .research_media import MEDIA_READ_TOOLS, ResearchMedia, media_tools

            operation = next(item[-1] for item in media_tools() if item[0] == name)
            def authorize(db):
                bot, run = self.runtime.leased(db, credential, run_id, lease)
                needed = "knowledge:read" if name in MEDIA_READ_TOOLS else "knowledge:write"
                if needed not in json.loads(bot["scopes_json"]):
                    raise StoreError(403, "bot attachment permissions changed")
                if name in {"media_read", "media_download_chunk"} and arguments.get("source_topic_id"):
                    self.runtime.access(db, bot, arguments["source_topic_id"])
                    self.store._topic_access(db, run["requested_by"], run["organization_id"], arguments["source_topic_id"])
            media = ResearchMedia(self.store, authorize=authorize)
            return getattr(media, operation)(context["owner_id"], scope["organization_id"], scope["topic_id"], **arguments)
        if name in {"messages_search", "topics_search"}:
            return BotConversations(self.runtime).invoke(credential, run_id, lease, name, arguments)
        if name == "topic_history":
            with self.store.transaction() as db:
                _, run = self.runtime.leased(db, credential, run_id, lease)
                upper = run["context_through_sequence"]
                before = arguments.get("before")
                rows = db.execute("SELECT m.*,u.name AS sender_name FROM messages m JOIN users u ON u.id=m.author_id "
                    "WHERE m.organization_id=? AND m.topic_id=? AND (? IS NULL OR m.sequence<=?) "
                    "AND (? IS NULL OR m.sequence<?) ORDER BY m.sequence DESC LIMIT ?",
                    (run["organization_id"], run["topic_id"], upper, upper, before, before, arguments.get("limit", 10))).fetchall()
                return [self.store._message(row, db) for row in reversed(rows)]
        if name.startswith("blog_"):
            with self.store.transaction(write=name not in {"blog_read", "blog_search"}) as db:
                bot, run = self.runtime.leased(db, credential, run_id, lease)
                topic = self.store._topic_access(db, bot["created_by"], run["organization_id"], run["topic_id"])[4]
                if "journal" not in json.loads(topic["knowledge_policy_json"]):
                    raise StoreError(403, "topic no longer grants blog access")
                needed = "knowledge:read" if name in {"blog_read", "blog_search"} else "knowledge:write"
                if needed not in json.loads(bot["scopes_json"]):
                    raise StoreError(403, "bot no longer grants blog access")
                from .research_tools import DEFAULT_TOOLS

                settings = db.execute("SELECT tools_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?",
                                      (run["organization_id"], run["topic_id"])).fetchone()
                if "knowledge" not in (json.loads(settings[0]) if settings else DEFAULT_TOOLS):
                    raise StoreError(403, "topic no longer grants knowledge tools")
                self.store._topic_access(db, run["requested_by"], run["organization_id"], run["topic_id"],
                                         write=name not in {"blog_read", "blog_search"})
                from .research_scope import KnowledgeWindow

                def source_access(connection, source_topic):
                    self.runtime.access(connection, bot, source_topic)
                    self.store._topic_access(connection, run["requested_by"], run["organization_id"], source_topic)

                window = KnowledgeWindow(self.store, db, bot["created_by"], run["organization_id"], run["topic_id"],
                                         arguments.get("scope"), topic_authorize=source_access)
                if name == "blog_search":
                    return window.search_entries({"journal"}, kind="journal", query=arguments.get("query", ""),
                                                 before=arguments.get("before"), published_only=False)
                if name == "blog_read":
                    entry = KnowledgeStore._view(db, run["organization_id"], arguments["entry_id"], arguments.get("revision"))
                    if entry["kind"] != "journal" or not window.entry_allowed(entry, exact=True):
                        raise StoreError(404, "project blog post unavailable")
                    return entry
                if arguments.get("entry_id"):
                    entry = KnowledgeStore._view(db, run["organization_id"], arguments["entry_id"])
                    if (entry["kind"] != "journal" or entry["project_id"] not in {None, run["project_id"]}
                        or entry["topic_id"] not in {None, run["topic_id"]}):
                        raise StoreError(404, "editable project blog post unavailable")
                values = dict(arguments)
                if name == "blog_publish":
                    values.update(title=entry["title"], body=entry["body"], state="published")
                values.setdefault("state", "draft")
                return KnowledgeStore(self.store)._write(db, bot["id"], "bot", run["organization_id"],
                    client_id="bot-" + run_id + "-" + client_id, kind="journal",
                    scope_kind="project" if name == "blog_create" else None,
                    scope_id=run["project_id"] if name == "blog_create" else None, **values)
        # Exact prior reads are retained locally by the gateway; writers still use the service's CAS checks.
        observations = RevisionObservations()
        with self.store.transaction() as db:
            self.runtime.leased(db, credential, run_id, lease)
            for row in db.execute("SELECT result_json FROM bot_tool_receipts WHERE run_id=? ORDER BY sequence", (run_id,)):
                result = json.loads(row[0])
                if result.get("current_revision_id") and result.get("claim_id"):
                    observations.note(result["claim_id"], result["current_revision_id"])
                if result.get("revision") and result.get("record"):
                    record, revision = result["record"], result["revision"]
                    for field in ("proof_id", "reference_id"):
                        if record.get(field) and revision.get("revision_id"):
                            observations.note(record[field], revision["revision_id"])
        read_scope = arguments.pop("scope", None) if name in READS else None
        result = MathematicalLedger(self.store).execute(context["owner_id"], scope["organization_id"], name, arguments,
            client_id="bot-" + run_id + "-" + client_id, project=scope["project_id"], topic=scope["topic_id"], agent=True,
            observations=observations, bot_invocation=(credential, run_id, lease), read_scope=read_scope)
        with self.store.transaction(write=True) as db:
            self.runtime.leased(db, credential, run_id, lease)
            db.execute("INSERT INTO bot_tool_receipts(run_id,name,result_json,created_at) VALUES(?,?,?,strftime('%s','now'))",
                       (run_id, name, json.dumps(result)))
        return result

    def knowledge(self, credential, run_id, lease, *, kind=None, query="", scope=None, before=None):
        self.runtime.bots._meter(credential, "read")
        with self.store.transaction() as db:
            bot, run = self.runtime.leased(db, credential, run_id, lease)
            if "knowledge:read" not in json.loads(bot["scopes_json"]):
                raise StoreError(403, "bot lacks shared knowledge access")
            topic = self.store._topic_access(db, bot["created_by"], run["organization_id"], run["topic_id"])[4]
            policy = set(json.loads(topic["knowledge_policy_json"]))
            if kind is not None and ("journal" if kind == "journal" else kind) not in policy:
                raise StoreError(403, "topic does not grant this knowledge collection")
            from .research_scope import KnowledgeWindow

            def source_access(connection, source_topic):
                self.runtime.access(connection, bot, source_topic)
                self.store._topic_access(connection, run["requested_by"], run["organization_id"], source_topic)

            window = KnowledgeWindow(self.store, db, bot["created_by"], run["organization_id"], run["topic_id"], scope,
                                     topic_authorize=source_access)
            return window.search_entries(policy, kind=kind, query=query, before=before)
