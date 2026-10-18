"""Authenticated beta goals, context inspection, tool grants and action review."""

import json
from typing import Annotated, Literal

from fastapi import Depends, Query
from pydantic import Field

from .request_input import RequestInput
from .research_context import ResearchContext
from .research_export import export_topic
from .research_goals import ResearchGoals
from .research_profiles import profile_catalog, topic_instructions
from .research_tools import DEFAULT_TOOLS, ResearchProposals
from .store import StoreError, _row


class GoalWrite(RequestInput):
    client_id: Annotated[str, Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
    objective: Annotated[str, Field(min_length=1, max_length=8000)]
    acceptance: Annotated[list[Annotated[str, Field(min_length=1, max_length=2000)]], Field(min_length=1, max_length=20)]
    model: Annotated[str, Field(min_length=1, max_length=200)]
    budget_microusd: Annotated[int, Field(ge=1, le=10**12, strict=True)]
    max_turns: Annotated[int, Field(ge=1, le=100, strict=True)] = 10
    max_tool_calls: Annotated[int, Field(ge=1, le=1000, strict=True)] = 100
    duration_seconds: Annotated[int, Field(ge=60, le=86400, strict=True)] = 3600
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh", "max"] | None = None


class GoalControl(RequestInput):
    status: Literal["active", "paused", "cancelled"]


class ToolSettings(RequestInput):
    tools: Annotated[list[Literal["history", "arxiv", "web", "knowledge", "workflows", "compute"]], Field(max_length=6)]
    reviewer_model: Annotated[str | None, Field(min_length=1, max_length=200)] = None
    review_enabled: Annotated[bool, Field(strict=True)] = True
    prompt_profile: Literal["workbench", "blueprint", "fact_graph", "author_critic", "lean"] = "workbench"
    additional_instructions: Annotated[str, Field(max_length=8000)] = ""
    context_target_percent: Annotated[int, Field(ge=40, le=80, strict=True)] = 60
    compute_allowance_cpu_seconds: Annotated[int, Field(ge=0, le=1000000, strict=True)] = 0


class ProposalReview(RequestInput):
    approve: bool


class PinWrite(RequestInput):
    pinned: bool


def install_routes(app, inference, compute, workflows, authenticated, *, enabled=True):
    store = inference.store
    goals, context = ResearchGoals(inference), ResearchContext(store)
    proposals = ResearchProposals(inference, compute, workflows)
    User = Annotated[str, Depends(authenticated)]
    prefix = "/api/organizations/{organization_id}/topics/{topic_id}"

    @app.get(prefix + "/agents")
    def topic_agents(organization_id: str, topic_id: str, user: User):
        from .topic_agents import TopicAgents

        return TopicAgents(inference).threads(user, organization_id, topic_id)

    @app.post(prefix + "/agents/control")
    def control_topic_agents(organization_id: str, topic_id: str, body: GoalControl, user: User):
        from .topic_agents import TopicAgents

        return TopicAgents(inference, compute).control_topic(user, organization_id, topic_id, body.status)

    @app.post(prefix + "/agents/{root_run}/control")
    def control_agents(organization_id: str, topic_id: str, root_run: str, body: GoalControl, user: User):
        from .topic_agents import TopicAgents

        return TopicAgents(inference, compute).control(user, organization_id, topic_id, root_run, body.status)

    @app.get(prefix + "/research-export")
    def diagnostics(organization_id: str, topic_id: str, user: User):
        return export_topic(inference, user, organization_id, topic_id)

    @app.get(prefix + "/export")
    def topic_diagnostics(organization_id: str, topic_id: str, user: User, detail: Literal["summary", "full", "raw"] = "summary"):
        from .topic_diagnostics import pack_trace, summarize_topic

        value = export_topic(inference, user, organization_id, topic_id, research_only=False)
        return pack_trace(value) if detail == "full" else summarize_topic(value) if detail == "summary" else value

    @app.get(prefix + "/research-instructions")
    def instructions(organization_id: str, topic_id: str, user: User):
        from .research_context import HARNESS_VERSION

        with store.transaction() as db:
            topic = store._topic_access(db, user, organization_id, topic_id)[4]
            if topic["kind"] != "beta":
                raise StoreError(400, "research instructions belong to beta topics")
            result = topic_instructions(db, organization_id, topic_id)
        return {**result, "harness_version": HARNESS_VERSION, "profiles": profile_catalog()}

    @app.get(prefix + "/goal")
    def goal(organization_id: str, topic_id: str, user: User):
        return goals.current(user, organization_id, topic_id)

    @app.post(prefix + "/goal", status_code=202)
    def create_goal(organization_id: str, topic_id: str, body: GoalWrite, user: User):
        from .bot_runtime import bot_model

        if not enabled and not bot_model(body.model):
            raise StoreError(503, "inference is disabled on this installation")
        if not bot_model(body.model):
            inference.preflight(user, organization_id, topic_id, [body.model])
        # Stable source identity: a lost reply never duplicates the goal message.
        store.post_message(
            user,
            organization_id,
            topic_id,
            "/goal " + body.objective + "\n\nAcceptance criteria:\n" + "\n".join("- " + item for item in body.acceptance),
            "goal-source-" + body.client_id[:110],
        )
        return goals.create(user, organization_id, topic_id, **body.model_dump())

    @app.patch(prefix + "/goal/{goal_id}")
    def control_goal(organization_id: str, topic_id: str, goal_id: str, body: GoalControl, user: User):
        return goals.control(user, organization_id, topic_id, goal_id, body.status)

    @app.get(prefix + "/research-context")
    def inspect_context(organization_id: str, topic_id: str, user: User, request_id: str | None = None):
        result = context.inspect(user, organization_id, topic_id, request_id)
        with store.transaction() as db:
            store._topic_access(db, user, organization_id, topic_id)
            result["memory_versions"] = [
                dict(row)
                for row in db.execute(
                    "SELECT revision,through_sequence,source_digest,run_id,created_at FROM research_memories "
                    "WHERE organization_id=? AND topic_id=? ORDER BY revision DESC LIMIT 40",
                    (organization_id, topic_id),
                )
            ]
            result["pins"] = [
                dict(row)
                for row in db.execute(
                    "SELECT m.id,m.sequence,m.content FROM research_pins p JOIN messages m ON m.id=p.message_id "
                    "WHERE p.organization_id=? AND p.topic_id=? ORDER BY m.sequence",
                    (organization_id, topic_id),
                )
            ]
            current = db.execute(
                "SELECT state_json FROM research_sessions WHERE organization_id=? AND topic_id=? ORDER BY updated_at DESC LIMIT 1",
                (organization_id, topic_id),
            ).fetchone()
            result["last_call_context"] = json.loads(current[0]) if current else None
        return result

    @app.put(prefix + "/research-pins/{message_id}", status_code=204)
    def pin(organization_id: str, topic_id: str, message_id: str, body: PinWrite, user: User):
        context.pin(user, organization_id, topic_id, message_id, body.pinned)

    @app.get(prefix + "/research-settings")
    def settings(organization_id: str, topic_id: str, user: User):
        with store.transaction() as db:
            topic = store._topic_access(db, user, organization_id, topic_id)[4]
            if topic["kind"] != "beta":
                raise StoreError(400, "research tools belong to beta topics")
            row = db.execute(
                "SELECT * FROM research_topic_settings WHERE organization_id=? AND topic_id=?", (organization_id, topic_id)
            ).fetchone()
            policy = json.loads(row["context_policy_json"]) if row else {}
            return {
                "tools": json.loads(row["tools_json"]) if row else DEFAULT_TOOLS,
                "reviewer_model": row["reviewer_model"] if row else None,
                "context_target_percent": json.loads(row["context_policy_json"]).get("target_percent", 60) if row else 60,
                "compute_allowance_cpu_seconds": row["compute_allowance_cpu_seconds"] if row else 0,
                "review_enabled": policy.get("review_enabled", True),
                "prompt_profile": policy.get("prompt_profile", "workbench"),
                "additional_instructions": policy.get("additional_instructions", ""),
                "profiles": profile_catalog(),
            }

    @app.put(prefix + "/research-settings")
    def set_settings(organization_id: str, topic_id: str, body: ToolSettings, user: User):
        if len(set(body.tools)) != len(body.tools):
            raise StoreError(400, "tool grants must be distinct")
        with store.transaction(write=True) as db:
            topic = store._topic_access(db, user, organization_id, topic_id, write=True)[4]
            if topic["kind"] != "beta":
                raise StoreError(400, "research tools belong to beta topics")
            db.execute(
                "INSERT INTO research_topic_settings(organization_id,topic_id,tools_json) VALUES(?,?,?) "
                "ON CONFLICT(organization_id,topic_id) "
                "DO UPDATE SET tools_json=excluded.tools_json",
                (organization_id, topic_id, json.dumps(sorted(body.tools))),
            )
            if "reviewer_model" in body.model_fields_set:
                db.execute(
                    "UPDATE research_topic_settings SET reviewer_model=? WHERE organization_id=? AND topic_id=?",
                    (body.reviewer_model, organization_id, topic_id),
                )
            policy_fields = {"context_target_percent": "target_percent", "review_enabled": "review_enabled",
                             "prompt_profile": "prompt_profile", "additional_instructions": "additional_instructions"}
            if body.model_fields_set & policy_fields.keys():
                policy = json.loads(db.execute("SELECT context_policy_json FROM research_topic_settings "
                                               "WHERE organization_id=? AND topic_id=?", (organization_id, topic_id)).fetchone()[0])
                for field, key in policy_fields.items():
                    if field in body.model_fields_set:
                        policy[key] = getattr(body, field)
                db.execute(
                    "UPDATE research_topic_settings SET context_policy_json=? WHERE organization_id=? AND topic_id=?",
                    (json.dumps(policy), organization_id, topic_id),
                )
            if "compute_allowance_cpu_seconds" in body.model_fields_set:
                db.execute(
                    "UPDATE research_topic_settings SET compute_allowance_cpu_seconds=? WHERE organization_id=? AND topic_id=?",
                    (body.compute_allowance_cpu_seconds, organization_id, topic_id),
                )
            store._audit(db, organization_id, user, "research.tools", topic_id)
        return {"tools": sorted(body.tools)}

    @app.get(prefix + "/mathematical-reviews")
    def mathematical_reviews(organization_id: str, topic_id: str, user: User):
        from .research_reviews import ResearchReviews

        return ResearchReviews(inference).list(user, organization_id, topic_id)

    @app.post(prefix + "/mathematical-reviews/{review_id}/cancel")
    def cancel_mathematical_review(organization_id: str, topic_id: str, review_id: str, user: User):
        from .research_reviews import ResearchReviews

        return ResearchReviews(inference).cancel(user, organization_id, topic_id, review_id)

    @app.get(prefix + "/research-proposals")
    def list_proposals(organization_id: str, topic_id: str, user: User):
        return proposals.list(user, organization_id, topic_id)

    @app.post(prefix + "/research-proposals/{proposal_id}")
    def review_proposal(organization_id: str, topic_id: str, proposal_id: str, body: ProposalReview, user: User):
        return proposals.review(user, organization_id, topic_id, proposal_id, body.approve)

    @app.get(prefix + "/research-tools")
    def receipts(organization_id: str, topic_id: str, user: User, after: Annotated[int, Query(ge=0)] = 0, summary: bool = False):
        with store.transaction() as db:
            store._topic_access(db, user, organization_id, topic_id)
            if summary:
                # Status lists omit arguments and results; GET /research-tools/{receipt_id} returns them.
                return [
                    dict(row)
                    for row in db.execute(
                        "SELECT r.id,r.name,r.status,r.created_at,r.finished_at "
                        "FROM research_tool_receipts r JOIN research_sessions s ON s.run_id=r.run_id "
                        "WHERE r.organization_id=? AND s.topic_id=? AND r.created_at>=? ORDER BY r.created_at DESC,r.id DESC LIMIT 50",
                        (organization_id, topic_id, after),
                    )
                ]
            return [
                dict(row)
                | {"arguments": json.loads(row["arguments_json"]), "result": json.loads(row["result_json"]) if row["result_json"] else None}
                for row in db.execute(
                    "SELECT r.* FROM research_tool_receipts r JOIN research_sessions s ON s.run_id=r.run_id "
                    "WHERE r.organization_id=? AND s.topic_id=? AND r.created_at>=? ORDER BY r.created_at DESC,r.id DESC LIMIT 50",
                    (organization_id, topic_id, after),
                )
            ]

    @app.get(prefix + "/research-tools/{receipt_id}")
    def receipt(organization_id: str, topic_id: str, receipt_id: str, user: User):
        with store.transaction() as db:
            store._topic_access(db, user, organization_id, topic_id)
            row = _row(
                db.execute(
                    "SELECT r.* FROM research_tool_receipts r JOIN research_sessions s ON s.run_id=r.run_id "
                    "WHERE r.organization_id=? AND s.topic_id=? AND r.id=?",
                    (organization_id, topic_id, receipt_id),
                ).fetchone()
            )
            return row | {
                "arguments": json.loads(row["arguments_json"]),
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
            }
