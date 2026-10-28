"""Research plans, private journal drafts, gamma authoring and sandbox admission."""

import json
import time
from datetime import UTC, datetime

from .knowledge_store import KnowledgeStore
from .research_context import encoded
from .store import StoreError


def register_actions(research):
    if not research.enabled:
        return
    from .research_tools import schema, string

    register = research.registry.tool

    @register(
        name="model_catalog",
        description="List current model choices. Any available OpenRouter model may be requested; "
        "live prices and capabilities are rechecked at admission.",
        input_schema=schema({}),
    )
    def model_catalog():
        with research.store.transaction() as db:
            research.store._require(db, research.user, research.org)
            policy = db.execute("SELECT model_policy_json FROM organizations WHERE id=?", (research.org,)).fetchone()
            models = getattr(research.store, "_model_catalog", json.loads(policy[0]))
        from .bot_runtime import BotRuntime
        from .bot_store import BotStore

        return {
            "allowed_models": models,
            "model_access": "all_available",
            "bot_models": BotRuntime(BotStore(research.store)).models(research.user, research.org, research.topic),
            "live_preflight_required": True,
        }

    if "knowledge" in research.enabled:

        @register(
            name="tasks",
            description="Read or replace this beta session’s durable task list. Planning state is not mathematical truth.",
            input_schema=schema(
                {
                    "tasks": {
                        "type": "array",
                        "maxItems": 20,
                        "items": schema(
                            {"title": string(500), "status": {"enum": ["pending", "in_progress", "completed"]}}, ["title", "status"]
                        ),
                    }
                }
            ),
        )
        def tasks(tasks=None):
            with research.store.transaction(write=tasks is not None) as db:
                research.store._topic_access(db, research.user, research.org, research.topic, write=tasks is not None)
                if tasks is not None:
                    db.execute(
                        "INSERT INTO research_topic_settings(organization_id,topic_id,tools_json,tasks_json) VALUES(?,?,?,?) "
                        "ON CONFLICT(organization_id,topic_id) DO UPDATE SET tasks_json=excluded.tasks_json",
                        (research.org, research.topic, encoded(sorted(research.enabled)), encoded(tasks)),
                    )
                row = db.execute(
                    "SELECT tasks_json FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (research.org, research.topic)
                ).fetchone()
                return {"tasks": json.loads(row[0]) if row else []}

    if "knowledge" in research.enabled and "journal" in research.collections:

        @register(
            name="blog_create",
            description="Create a project blog draft or publish an authorized post. "
            "The audience remains permission-scoped; cite evidence IDs.",
            risk="write",
            input_schema=schema({"title": string(300), "body": string(16000),
                                 "state": {"enum": ["draft", "published"]}}, ["title", "body"]),
        )
        def blog_create(title, body, state="draft"):
            with research.store.transaction(write=True) as db:
                research.store._topic_access(db, research.user, research.org, research.topic, write=True)
                return KnowledgeStore(research.store)._write(
                    db,
                    research.user,
                    "bot",
                    research.org,
                    client_id="agent-" + research.row["id"] + "-" + str(research.sequence),
                    kind="journal",
                    title=title,
                    body=body,
                    state=state,
                    scope_kind="project",
                    scope_id=research.project,
                )

        @register(
            name="blog_revise",
            description="Edit an existing draft or published project blog post using its current head revision. "
            "State draft retains the published snapshot; state published publishes this revision immediately.",
            risk="write",
            input_schema=schema(
                {
                    "entry_id": string(32),
                    "title": string(300),
                    "body": string(16000),
                    "expected_revision": {"type": "integer", "minimum": 1},
                    "state": {"enum": ["draft", "published"]},
                },
                ["entry_id", "title", "body", "expected_revision"],
            ),
        )
        def blog_revise(entry_id, title, body, expected_revision, state="draft"):
            with research.store.transaction(write=True) as db:
                research.store._topic_access(db, research.user, research.org, research.topic, write=True)
                entry = KnowledgeStore(research.store)._view(db, research.org, entry_id)
                if (entry["kind"] != "journal" or entry["project_id"] not in {None, research.project}
                    or entry["topic_id"] not in {None, research.topic}):
                    raise StoreError(404, "editable blog draft unavailable")
                return KnowledgeStore(research.store)._write(
                    db,
                    research.user,
                    "bot",
                    research.org,
                    client_id="agent-" + research.row["id"] + "-" + str(research.sequence),
                    kind="journal",
                    title=title,
                    body=body,
                    state=state,
                    entry_id=entry_id,
                    expected_revision=expected_revision,
                )

        @register(
            name="blog_publish",
            description="Publish the exact current project blog revision. Use only when the user authorizes publication; "
            "the audience remains private to authorized project members.",
            risk="write",
            input_schema=schema(
                {"entry_id": string(32), "expected_revision": {"type": "integer", "minimum": 1}}, ["entry_id", "expected_revision"]
            ),
        )
        def blog_publish(entry_id, expected_revision):
            with research.store.transaction(write=True) as db:
                research.store._topic_access(db, research.user, research.org, research.topic, write=True)
                entry = KnowledgeStore._view(db, research.org, entry_id)
                if (entry["kind"] != "journal" or entry["project_id"] not in {None, research.project}
                    or entry["topic_id"] not in {None, research.topic}):
                    raise StoreError(404, "editable project blog post unavailable")
                return KnowledgeStore(research.store)._write(db, research.user, "bot", research.org,
                    client_id="agent-" + research.row["id"] + "-" + str(research.sequence), kind="journal",
                    entry_id=entry_id, title=entry["title"], body=entry["body"], state="published",
                    expected_revision=expected_revision)

        @register(
            name="blog_read",
            description="Read an exact private research-blog revision in this project, including drafts; returns attribution and revision.",
            input_schema=schema({"entry_id": string(32), "revision": {"type": "integer", "minimum": 1}}, ["entry_id"]),
        )
        def blog_read(entry_id, revision=None):
            entry = KnowledgeStore(research.store).get(research.user, research.org, entry_id, revision)
            if entry["kind"] != "journal" or not research._knowledge_allowed(entry, exact=True, allow_draft=True):
                raise StoreError(404, "blog post unavailable to this topic")
            return entry

        @register(
            name="blog_search",
            description="Search private research-blog titles and posts within this topic’s allowed project and organization scope.",
            input_schema=schema({"query": {"type": "string", "maxLength": 200},
                                 "scope": {"enum": ["working_set", "topic", "project"]}, "before": string(100)}),
        )
        def blog_search(query="", scope=None, before=None):
            from .research_scope import KnowledgeWindow

            with research.store.transaction() as db:
                return KnowledgeWindow(research.store, db, research.user, research.org, research.topic, scope).search_entries(
                    research.collections, kind="journal", query=query, before=before, published_only=False)

    if "workflows" in research.enabled and "workflows" in research.collections and research.workflows:

        @register(
            name="workflow_read",
            description="Read an exact gamma workflow version in this project.",
            input_schema=schema({"workflow_id": string(32), "version": {"type": "integer", "minimum": 1}}, ["workflow_id"]),
        )
        def workflow_read(workflow_id, version=None):
            saved = research.workflows.definitions.get(research.user, research.org, workflow_id, version)
            research._target(saved["topic_id"])
            return saved

        @register(
            name="workflow_create",
            description="Create one complete immutable gamma workflow draft in this project. Draft creation does not authorize execution.",
            risk="write",
            input_schema=schema({"topic_id": string(32), "definition": {"type": "object"}}, ["topic_id", "definition"]),
        )
        def workflow_create(topic_id, definition):
            if research._target(topic_id)["kind"] != "gamma":
                raise StoreError(400, "workflow drafts belong to gamma topics")
            return research.workflows.definitions.create(
                research.user, research.org, topic_id, "agent-" + research.row["id"] + "-" + str(research.sequence), definition
            )

        @register(
            name="workflow_update",
            description="Save an immutable gamma workflow revision after reading its exact current version. Never run it implicitly.",
            risk="write",
            input_schema=schema(
                {"workflow_id": string(32), "expected_version": {"type": "integer", "minimum": 1}, "definition": {"type": "object"}},
                ["workflow_id", "expected_version", "definition"],
            ),
        )
        def workflow_update(workflow_id, expected_version, definition):
            saved = workflow_read(workflow_id)
            if expected_version != saved["version"]:
                raise StoreError(409, "workflow changed; read its current version")
            return research.workflows.definitions.revise(
                research.user,
                research.org,
                workflow_id,
                "agent-" + research.row["id"] + "-" + str(research.sequence),
                expected_version,
                definition,
            )

        @register(
            name="workflow_preflight",
            description="Validate a gamma version and current image/model policy without running it. Live pricing happens at admission.",
            input_schema=schema({"workflow_id": string(32), "version": {"type": "integer", "minimum": 1}}, ["workflow_id", "version"]),
        )
        def preflight(workflow_id, version):
            saved = workflow_read(workflow_id, version)
            with research.store.transaction() as db:
                research.workflows._admission(db, research.user, research.org, saved["topic_id"], saved["definition"])
            return {
                "workflow_id": workflow_id,
                "version": version,
                "definition_digest": saved["definition_digest"],
                "policy_preflight": "passed",
                "provider_prices": "rechecked at admission",
                "creates_paid_jobs": False,
            }

    if "compute" in research.enabled and research.compute:

        @register(
            name="compute_submit",
            description=(
                "Submit bounded code to an approved sandbox. Uses the topic CPU allowance or proposes exact-action approval. "
                "Sage/Python/GAP/PARI/Julia/Lean require advertised image support."
            ),
            risk="external",
            input_schema=schema(
                {"image_id": string(64), "language": string(32), "program": string(16000), "resources": {"type": "object"}},
                ["image_id", "language", "program", "resources"],
            ),
        )
        def compute_submit(image_id, language, program, resources):
            parameters = research.compute.parameters(image_id, language, program, resources)
            with research.store.transaction(write=True) as db:
                research.store._topic_access(db, research.user, research.org, research.topic, write=True)
                settings = db.execute(
                    "SELECT compute_allowance_cpu_seconds FROM research_topic_settings WHERE organization_id=? AND topic_id=?",
                    (research.org, research.topic),
                ).fetchone()
                allowance = settings[0] if settings else 0
                month = int(datetime.fromtimestamp(time.time(), UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
                held = db.execute(
                    "SELECT COALESCE(SUM(CASE WHEN accounted_at IS NULL THEN reserved_cpu_seconds "
                    "WHEN accounted_at>=? THEN used_cpu_seconds ELSE 0 END),0) "
                    "FROM compute_jobs WHERE organization_id=? AND topic_id=?",
                    (month, research.org, research.topic),
                ).fetchone()[0]
                required = resources["cpu"] * resources["wall_time_seconds"]
                if required <= allowance - held:
                    return research.compute._enqueue(
                        db,
                        research.user,
                        research.org,
                        research.topic,
                        "agent-" + research.row["id"] + "-" + str(research.sequence),
                        parameters,
                    )
            return research._proposal({"action": "compute", "topic_id": research.topic, "parameters": parameters})

        @register(
            name="compute_cancel",
            description="Cancel a sandbox job in this project. Cancellation does not prove the job was free or cleanup completed.",
            input_schema=schema({"job_id": string(32)}, ["job_id"]),
        )
        def compute_cancel(job_id):
            job = research.compute.job(research.user, research.org, job_id)
            research._target(job["topic_id"])
            return research.compute.cancel(research.user, research.org, job_id)

        from .noether_compute import register_jobs

        register_jobs(research.registry, compute_submit)

        @register(
            name="lean_check",
            description="Check Lean source in an approved sandbox with warnings as errors. Success does not certify a prose claim.",
            input_schema=schema(
                {"image_id": string(64), "program": string(15000), "resources": {"type": "object"}}, ["image_id", "program", "resources"]
            ),
        )
        def lean_check(image_id, program, resources):
            return compute_submit(image_id, "lean", program, resources)

        @register(
            name="lean_project_check",
            description="Compile a pinned offline mathlib project and audit a named target against an explicit Lean type. "
            "Reports are job artifacts, not proof certification of a prose claim. Requires a lean-mathlib image; "
            "request at least 4294967296 memory_bytes for typical mathlib audits, with compatible worker capacity.",
            input_schema=schema(
                {
                    "image_id": string(64),
                    "files": {"type": "object", "minProperties": 1, "maxProperties": 32, "additionalProperties": string(15000)},
                    "entry": string(200),
                    "target": string(200),
                    "expected_type": string(2000),
                    "resources": {"type": "object"},
                },
                ["image_id", "files", "entry", "target", "expected_type", "resources"],
            ),
        )
        def lean_project_check(image_id, files, entry, target, expected_type, resources):
            import json

            program = json.dumps(
                {"format": 1, "files": files, "entry": entry, "target": target, "expected_type": expected_type}, ensure_ascii=False
            )
            if len(program) > 16000:
                raise ValueError("Lean project exceeds the compute program allowance")
            return compute_submit(image_id, "lean", program, resources)
