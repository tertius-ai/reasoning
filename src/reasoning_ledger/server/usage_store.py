"""Organization-scoped LLM accounting and explicitly configured operational estimates."""

from __future__ import annotations

import hashlib
import json
import time

from .openrouter import Quote, Usage, safe_diagnostics
from .store import Store, StoreError

REFERENCE_KWH_PER_MILLION = 1 / 3.6  # Illustrative 1 J per processed token, not a provider measurement.
REFERENCE_PROFILE = {
    "model": "Generic reference scenario, not the requested model",
    "input_kwh_per_million": REFERENCE_KWH_PER_MILLION,
    "output_kwh_per_million": REFERENCE_KWH_PER_MILLION,
    "location": "Inference location not reported",
    "grid_grams_co2e_per_kwh": None,
    "source": "Reasoning reference scenario v1: assumed 1 J per input/output token. Not calibrated to this model or provider. "
    "Energy-per-processed-token metric: Wilhelm et al., EuroMLSys 2025, https://arxiv.org/abs/2603.20224. "
    "The coefficient is an illustrative application assumption, not a value reported by the paper. GPU-only; no cooling/PUE overhead.",
}


def footprint(row) -> dict:
    configured = bool(row["impact_profile_json"])
    profile = json.loads(row["impact_profile_json"]) if configured else dict(REFERENCE_PROFILE)
    prompt, completion = row["prompt_tokens"], row["completion_tokens"]
    energy = None
    if profile is not None and prompt is not None and completion is not None:
        energy = (prompt * profile["input_kwh_per_million"] + completion * profile["output_kwh_per_million"]) / 1_000_000
    carbon = None if energy is None or profile["grid_grams_co2e_per_kwh"] is None else energy * profile["grid_grams_co2e_per_kwh"] / 1000
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "actual_microusd": row["actual_microusd"],
        "estimated_kwh": energy,
        "estimated_kg_co2e": carbon,
        "assumptions": profile,
        "estimate_kind": "configured" if configured else "reference",
    }


class UsageStore:
    def __init__(self, store: Store):
        self.store = store

    @staticmethod
    def _receipt(db, org: str, call: str, kind: str, data: dict) -> None:
        db.execute(
            "INSERT INTO inference_call_events(organization_id,call_id,kind,data_json,recorded_at) VALUES(?,?,?,?,?)",
            (org, call, kind, json.dumps(data, allow_nan=False), int(time.time())),
        )

    def begin_call(
        self,
        row: dict,
        quote: Quote,
        *,
        purpose: str = "answer",
        parent_call_id: str | None = None,
        harness_version: str = "stream-v1",
        prompt_version: str = "shared-conversation-v1",
        context: list[dict] | None = None,
        reserved_microusd: int | None = None,
        route: dict | None = None,
        preparation=None,
    ) -> str:
        """Write the immutable receipt before submission. Caller persists the ID.

        ``preparation`` holds the call's provider-stage spans; the submission
        receipt retains their names and durations only.
        """
        from .store import _id

        call = _id()
        messages = context if context is not None else json.loads(row["context_json"])
        digest = hashlib.sha256(json.dumps(messages, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        reserved = row["reserved_microusd"] if reserved_microusd is None else reserved_microusd
        if preparation is not None:
            preparation.mark("call_ledger")
        with self.store.transaction(write=True) as db:
            if preparation is not None:
                preparation.mark("call_ledger_lock")
            project = db.execute(
                "SELECT c.project_id FROM topics t JOIN channels c ON c.id=t.channel_id WHERE t.id=?", (row["topic_id"],)
            ).fetchone()[0]
            if (
                parent_call_id is not None
                and db.execute(
                    "SELECT 1 FROM inference_calls WHERE id=? AND organization_id=? AND run_id=?",
                    (parent_call_id, row["organization_id"], row["id"]),
                ).fetchone()
                is None
            ):
                raise ValueError("parent provider call must belong to this run")
            db.execute(
                """INSERT INTO inference_calls(id,organization_id,run_id,request_id,requested_by,topic_id,project_id,model,
                parent_call_id,purpose,harness_version,prompt_version,quote_json,route_json,context_sha256,reserved_microusd,
                started_at,started_at_ms,source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'executor')""",
                (
                    call,
                    row["organization_id"],
                    row["id"],
                    row["request_id"],
                    row["requested_by"],
                    row["topic_id"],
                    project,
                    row["model"],
                    parent_call_id,
                    purpose,
                    harness_version,
                    prompt_version,
                    json.dumps(quote.record()),
                    json.dumps(route or quote.route() | {"region": (quote.routing or {}).get("region", "global")}),
                    digest,
                    reserved,
                    int(time.time()),
                    int(time.time() * 1000),
                ),
            )
            submission = {"status": "running", "accounting_state": "reserved"}
            if preparation is not None:
                preparation.mark("call_ledger")
                submission["preparation_ms"] = preparation.record()
            self._receipt(db, row["organization_id"], call, "submission", submission)
        return call

    def first_token(self, call_id: str) -> None:
        """Record time to the first content, reasoning or tool delta, once per call."""
        with self.store.transaction(write=True) as db:
            header = db.execute("SELECT organization_id,started_at_ms FROM inference_calls WHERE id=?", (call_id,)).fetchone()
            if header is None:
                raise ValueError("unknown provider call")
            if db.execute("SELECT 1 FROM inference_call_events WHERE call_id=? AND kind IN ('first_token','completion')",
                          (call_id,)).fetchone():
                return
            response = db.execute("SELECT data_json FROM inference_call_events WHERE call_id=? AND kind='response_headers'",
                                  (call_id,)).fetchone()
            self._receipt(db, header[0], call_id, "first_token", {
                "status": "running", "accounting_state": "reserved",
                "time_to_first_token_ms": max(0, int(time.time() * 1000) - header[1]) if header[1] is not None else None,
                "time_to_response_headers_ms": json.loads(response[0])["time_to_response_headers_ms"] if response else None,
            })

    def response_headers(self, call_id: str, *, dispatch: dict | None = None) -> None:
        """Record HTTP response arrival, not an invented network-only latency.

        ``dispatch`` holds content-free span durations between the call start and the request.
        """
        with self.store.transaction(write=True) as db:
            header = db.execute("SELECT organization_id,started_at_ms FROM inference_calls WHERE id=?", (call_id,)).fetchone()
            if header is None:
                raise ValueError("unknown provider call")
            if db.execute("SELECT 1 FROM inference_call_events WHERE call_id=? AND kind IN ('response_headers','completion')",
                          (call_id,)).fetchone():
                return
            receipt = {
                "status": "running", "accounting_state": "reserved",
                "time_to_response_headers_ms": max(0, int(time.time() * 1000) - header[1]) if header[1] is not None else None,
            }
            if dispatch:
                receipt["dispatch_ms"] = dispatch
            self._receipt(db, header[0], call_id, "response_headers", receipt)

    def finish_call(
        self,
        call_id: str,
        *,
        status: str,
        usage: Usage | None = None,
        provider_generation_id: str | None = None,
        diagnostics: dict | None = None,
        no_charge: bool = False,
        error_code: str | None = None,
    ) -> None:
        diagnostics = safe_diagnostics(diagnostics)
        with self.store.transaction(write=True) as db:
            header = db.execute("SELECT organization_id,started_at_ms FROM inference_calls WHERE id=?", (call_id,)).fetchone()
            if header is None:
                raise ValueError("unknown provider call")
            if db.execute("SELECT 1 FROM inference_call_events WHERE call_id=? AND kind='completion'", (call_id,)).fetchone():
                return
            timing = db.execute("SELECT data_json FROM inference_call_events WHERE call_id=? AND kind='first_token'",
                                (call_id,)).fetchone()
            headers = db.execute("SELECT data_json FROM inference_call_events WHERE call_id=? AND kind='response_headers'",
                                 (call_id,)).fetchone()
            submission = db.execute("SELECT data_json FROM inference_call_events WHERE call_id=? AND kind='submission'",
                                    (call_id,)).fetchone()
            spans = {
                "preparation_ms": json.loads(submission[0]).get("preparation_ms") if submission else None,
                "dispatch_ms": json.loads(headers[0]).get("dispatch_ms") if headers else None,
            }
            self._receipt(
                db,
                header[0],
                call_id,
                "completion",
                {
                    "status": status,
                    "accounting_state": "settled" if usage else "released" if no_charge else "uncertain",
                    "actual_microusd": usage.cost_microusd if usage else None,
                    "prompt_tokens": usage.prompt_tokens if usage else None,
                    "completion_tokens": usage.completion_tokens if usage else None,
                    "cached_tokens": usage.cached_tokens if usage else None,
                    "reasoning_tokens": usage.reasoning_tokens if usage else None,
                    "total_tokens": usage.total_tokens if usage else None,
                    "tool_tokens": usage.tool_tokens if usage else None,
                    "provider_generation_id": provider_generation_id,
                    "diagnostics": diagnostics,
                    "error_code": error_code,
                    "finished_at": int(time.time()),
                    "latency_ms": max(0, int(time.time() * 1000) - header[1]) if header[1] is not None else None,
                    "time_to_first_token_ms": json.loads(timing[0])["time_to_first_token_ms"] if timing else None,
                    "time_to_response_headers_ms": json.loads(headers[0])["time_to_response_headers_ms"] if headers else None,
                    **{key: value for key, value in spans.items() if value is not None},
                },
            )

    def reconcile_calls(self, db, org: str, run_id: str, cost: int, actor: str) -> None:
        """Append verified accounting; never rewrite the original submission evidence."""
        calls = db.execute(
            "SELECT id FROM inference_calls WHERE organization_id=? AND run_id=? ORDER BY sequence", (org, run_id)
        ).fetchall()
        unresolved = []
        known = 0
        for call in calls:
            last = db.execute(
                "SELECT data_json FROM inference_call_events WHERE call_id=? ORDER BY sequence DESC LIMIT 1", (call[0],)
            ).fetchone()
            data = json.loads(last[0])
            if data.get("accounting_state") in {"uncertain", "reserved"}:
                unresolved.append((call[0], data))
            else:
                known += data.get("actual_microusd") or 0
        if len(unresolved) > 1:
            raise StoreError(409, "multiple uncertain provider calls: reconcile each call from its provider record")
        if unresolved:
            call, data = unresolved[0]
            if cost < known:
                raise StoreError(400, "verified run cost is below already known provider charges")
            self._receipt(
                db,
                org,
                call,
                "reconciliation",
                {
                    **data,
                    "accounting_state": "settled",
                    "actual_microusd": cost - known,
                    "reconciled_by": actor,
                    "reconciled_at": int(time.time()),
                },
            )

    @staticmethod
    def diagnostics(db, run_id: str) -> dict | None:
        row = db.execute(
            """SELECT e.data_json FROM inference_calls i JOIN inference_call_events e ON e.call_id=i.id
            WHERE i.run_id=? AND json_extract(e.data_json,'$.diagnostics') IS NOT NULL ORDER BY e.sequence DESC LIMIT 1""",
            (run_id,),
        ).fetchone()
        return json.loads(row[0]).get("diagnostics") if row else None

    def recover_calls(self) -> None:
        """Append an uncertain interruption receipt after abandoned runs are fenced."""
        with self.store.transaction() as db:
            calls = [
                row[0]
                for row in db.execute("""SELECT i.id FROM inference_calls i
                JOIN llm_runs r ON r.id=i.run_id JOIN inference_call_events e ON e.sequence=(
                    SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id)
                WHERE r.status NOT IN ('queued','running') AND json_extract(e.data_json,'$.status')='running'""")
            ]
        for call in calls:
            self.finish_call(call, status="interrupted", error_code="worker_interrupted")

    def reconcile_call(self, user: str, org: str, call_id: str, cost: int) -> None:
        if type(cost) is not int or not 0 <= cost <= 10**15:
            raise StoreError(400, "invalid verified provider cost")
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            row = db.execute("SELECT run_id FROM inference_calls WHERE organization_id=? AND id=?", (org, call_id)).fetchone()
            if row is None:
                raise StoreError(404, "provider call not found")
            run_id = row[0]
            run = db.execute("SELECT status FROM llm_runs WHERE id=?", (run_id,)).fetchone()
            if run[0] in {"queued", "running"}:
                raise StoreError(409, "wait for the run to stop before reconciling a provider call")
            last = db.execute(
                "SELECT data_json FROM inference_call_events WHERE call_id=? ORDER BY sequence DESC LIMIT 1", (call_id,)
            ).fetchone()
            data = json.loads(last[0])
            if data.get("accounting_state") not in {"uncertain", "reserved"}:
                raise StoreError(409, "only uncertain provider calls can be reconciled")
            self._receipt(
                db,
                org,
                call_id,
                "reconciliation",
                {**data, "accounting_state": "settled", "actual_microusd": cost, "reconciled_by": user, "reconciled_at": int(time.time())},
            )
            receipts = [
                json.loads(item[0])
                for item in db.execute(
                    """SELECT e.data_json FROM inference_calls i
                JOIN inference_call_events e ON e.sequence=(SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id)
                WHERE i.run_id=?""",
                    (run_id,),
                )
            ]
            uncertain = any(item.get("accounting_state") in {"uncertain", "reserved"} for item in receipts)
            actual = None if uncertain else sum(item.get("actual_microusd") or 0 for item in receipts)
            db.execute(
                "UPDATE llm_runs SET accounting_state=?,actual_microusd=?,settled_at=? WHERE id=?",
                ("uncertain" if uncertain else "settled", actual, None if uncertain else int(time.time()), run_id),
            )
            self.store._audit(db, org, user, "inference.call-cost-reconcile", call_id)

    def recommendation_preferences(self, user: str, org: str) -> list[str] | None:
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            row = db.execute("SELECT model_ids_json FROM model_recommendation_preferences WHERE organization_id=?", (org,)).fetchone()
            return json.loads(row[0]) if row else None

    def set_recommendations(self, user: str, org: str, model_ids: list[str] | None) -> None:
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            if model_ids is None:
                db.execute("DELETE FROM model_recommendation_preferences WHERE organization_id=?", (org,))
            else:
                if (
                    len(set(model_ids)) != len(model_ids)
                    or len(model_ids) > 20
                    or any(not isinstance(model, str) or not 1 <= len(model) <= 200 for model in model_ids)
                ):
                    raise StoreError(400, "recommendations must be at most 20 distinct model IDs")
                db.execute(
                    "INSERT INTO model_recommendation_preferences VALUES(?,?) "
                    "ON CONFLICT(organization_id) DO UPDATE SET model_ids_json=excluded.model_ids_json",
                    (org, json.dumps(model_ids)),
                )
            self.store._audit(db, org, user, "inference.recommendations", org)

    @staticmethod
    def _call_query() -> str:
        return """SELECT i.*,u.name AS actor_name,t.name AS topic_name,c.name AS channel_name,p.name AS project_name,
            e.data_json,r.impact_profile_json,w.run_id AS workflow_run_id,w.node_id AS workflow_node_id
            FROM inference_calls i JOIN users u ON u.id=i.requested_by
            JOIN llm_runs r ON r.id=i.run_id
            JOIN topics t ON t.id=i.topic_id JOIN channels c ON c.id=t.channel_id JOIN projects p ON p.id=i.project_id
            JOIN inference_call_events e ON e.sequence=(SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id)
            LEFT JOIN workflow_node_attempts w ON w.inference_run_id=i.run_id
            LEFT JOIN project_memberships pm ON pm.project_id=p.id AND pm.user_id=?
            WHERE i.organization_id=? AND (p.visibility='organization' OR ? IN ('owner','admin') OR pm.user_id IS NOT NULL)
            AND p.deleted_at IS NULL AND c.deleted_at IS NULL AND t.deleted_at IS NULL """

    @staticmethod
    def _call_view(row) -> dict:
        value = dict(row)
        data = json.loads(value.pop("data_json"))
        value["quote"] = json.loads(value.pop("quote_json"))
        value["route"] = json.loads(value.pop("route_json"))
        usage = footprint(
            {
                **value,
                "prompt_tokens": data.get("prompt_tokens"),
                "completion_tokens": data.get("completion_tokens"),
                "actual_microusd": data.get("actual_microusd"),
            }
        )
        value.pop("impact_profile_json")
        return {**value, **data, "usage": usage}

    def calls(
        self,
        user: str,
        org: str,
        *,
        before: int | None = None,
        limit: int = 100,
        project_id: str | None = None,
        topic_id: str | None = None,
        model: str | None = None,
        actor: str | None = None,
        status: str | None = None,
        purpose: str | None = None,
        since: int | None = None,
        until: int | None = None,
    ) -> dict:
        with self.store.transaction() as db:
            role = self.store._require(db, user, org)
            if project_id:
                self.store._project_access(db, user, org, project_id)
            if topic_id:
                self.store._topic_access(db, user, org, topic_id)
            query, args = self._call_query(), [user, org, role]
            for column, value in (
                ("i.project_id", project_id),
                ("i.topic_id", topic_id),
                ("i.model", model),
                ("i.requested_by", actor),
                ("i.purpose", purpose),
                ("json_extract(e.data_json,'$.status')", status),
            ):
                if value is not None:
                    query += f" AND {column}=?"
                    args.append(value)
            for column, op, value in (("i.sequence", "<", before), ("i.started_at", ">=", since), ("i.started_at", "<=", until)):
                if value is not None:
                    query += f" AND {column}{op}?"
                    args.append(value)
            rows = [self._call_view(row) for row in db.execute(query + " ORDER BY i.sequence DESC LIMIT ?", [*args, limit + 1])]
            more = len(rows) > limit
            rows = rows[:limit]
            return {"calls": rows, "next_before": rows[-1]["sequence"] if more else None}

    def call(self, user: str, org: str, call_id: str) -> dict:
        with self.store.transaction() as db:
            role = self.store._require(db, user, org)
            row = db.execute(self._call_query() + " AND i.id=?", (user, org, role, call_id)).fetchone()
            if row is None:
                raise StoreError(404, "provider call not found")
            receipts = [
                {**dict(event), "data": json.loads(event["data_json"])}
                for event in db.execute(
                    "SELECT sequence,kind,recorded_at,data_json FROM inference_call_events WHERE call_id=? ORDER BY sequence", (call_id,)
                )
            ]
            for receipt in receipts:
                receipt.pop("data_json")
            return {**self._call_view(row), "receipts": receipts}

    def profiles(self, user: str, org: str) -> list[dict]:
        with self.store.transaction() as db:
            self.store._require(db, user, org)
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT profile_json FROM model_impact_profiles WHERE organization_id=? ORDER BY model",
                    (org,),
                )
            ]

    def set_profiles(self, user: str, org: str, profiles: list[dict]) -> list[dict]:
        if len({profile["model"] for profile in profiles}) != len(profiles):
            raise StoreError(400, "each model needs at most one impact profile")
        with self.store.transaction(write=True) as db:
            self.store._require(db, user, org, admin=True)
            db.execute("DELETE FROM model_impact_profiles WHERE organization_id=?", (org,))
            db.executemany(
                "INSERT INTO model_impact_profiles VALUES(?,?,?)",
                [(org, profile["model"], json.dumps(profile, allow_nan=False)) for profile in profiles],
            )
            self.store._audit(db, org, user, "model_impact_profiles.update", org)
        return profiles

    def navigation_totals(self, user: str, org: str) -> dict:
        """Confirmed provider charges once per call; externally billed bots stay separate."""
        with self.store.transaction() as db:
            role = self.store._require(db, user, org)
            rows = db.execute("""WITH accounted AS (
                SELECT i.topic_id,
                  CASE WHEN json_extract(e.data_json,'$.accounting_state')='settled'
                    THEN COALESCE(json_extract(e.data_json,'$.actual_microusd'),0) ELSE 0 END AS cost,
                  CASE WHEN json_extract(e.data_json,'$.accounting_state') IN ('reserved','uncertain')
                    THEN 1 ELSE 0 END AS unknown_calls, 0 AS external_runs
                FROM inference_calls i JOIN inference_call_events e ON e.sequence=(
                    SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id)
                WHERE i.organization_id=?
                UNION ALL
                SELECT q.topic_id,CASE WHEN r.accounting_state='settled' THEN COALESCE(r.actual_microusd,0) ELSE 0 END,
                    CASE WHEN r.accounting_state IN ('reserved','uncertain') THEN 1 ELSE 0 END,0
                FROM llm_runs r JOIN llm_requests q ON q.id=r.request_id AND q.organization_id=r.organization_id
                WHERE r.organization_id=? AND NOT EXISTS(SELECT 1 FROM inference_calls i WHERE i.run_id=r.id)
                UNION ALL
                SELECT topic_id,0,CASE WHEN status='outcome_unknown' THEN 1 ELSE 0 END,1
                FROM bot_runs WHERE organization_id=?
            ) SELECT p.id AS project_id,c.id AS channel_id,t.id AS topic_id,
                COALESCE(SUM(a.cost),0) AS actual_microusd,
                COALESCE(SUM(a.unknown_calls),0) AS unknown_calls,
                COALESCE(SUM(a.external_runs),0) AS external_runs
            FROM topics t JOIN channels c ON c.id=t.channel_id AND c.organization_id=t.organization_id
            JOIN projects p ON p.id=c.project_id AND p.organization_id=c.organization_id
            LEFT JOIN project_memberships pm ON pm.project_id=p.id AND pm.organization_id=p.organization_id AND pm.user_id=?
            LEFT JOIN accounted a ON a.topic_id=t.id
            WHERE t.organization_id=? AND (p.visibility='organization' OR ? IN ('owner','admin') OR pm.user_id IS NOT NULL)
            GROUP BY p.id,c.id,t.id""", (org, org, org, user, org, role)).fetchall()
            result = {"projects": {}, "channels": {}, "topics": {}}
            for row in rows:
                for kind, field in (("projects", "project_id"), ("channels", "channel_id"), ("topics", "topic_id")):
                    total = result[kind].setdefault(row[field], {"actual_microusd": 0, "unknown_calls": 0, "external_runs": 0})
                    for measure in total:
                        total[measure] += row[measure]
            return result

    def key_totals(self, user: str, org: str) -> list[dict]:
        """Administrator accounting for this application's calls, never key secrets."""
        with self.store.transaction() as db:
            self.store._require(db, user, org, admin=True)
            rows = db.execute("""WITH accounted AS (
                SELECT i.run_id,i.reserved_microusd,
                  json_extract(e.data_json,'$.actual_microusd') AS actual_microusd,
                  json_extract(e.data_json,'$.accounting_state') AS accounting_state
                FROM inference_calls i JOIN inference_call_events e ON e.sequence=(
                    SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id)
                WHERE i.organization_id=?
                UNION ALL
                SELECT id,reserved_microusd,actual_microusd,accounting_state FROM llm_runs r
                WHERE r.organization_id=? AND NOT EXISTS(SELECT 1 FROM inference_calls i WHERE i.run_id=r.id)
            ), attributed AS (
                SELECT r.key_id,a.* FROM accounted a JOIN llm_runs r ON r.id=a.run_id
                WHERE r.organization_id=?
            ) SELECT k.id AS key_id,k.label,k.disabled_at,COUNT(a.run_id) AS calls,
                COALESCE(SUM(a.actual_microusd),0) AS actual_microusd,
                COALESCE(SUM(CASE WHEN a.accounting_state='reserved' THEN a.reserved_microusd ELSE 0 END),0)
                  AS reserved_microusd,
                COALESCE(SUM(CASE WHEN a.accounting_state='uncertain' THEN a.reserved_microusd ELSE 0 END),0)
                  AS uncertain_microusd,
                COALESCE(SUM(CASE WHEN a.accounting_state='uncertain' THEN 1 ELSE 0 END),0) AS unknown_calls
                FROM provider_keys k LEFT JOIN attributed a ON a.key_id=k.id
                WHERE k.organization_id=? GROUP BY k.id,k.label,k.disabled_at ORDER BY k.label,k.id""",
                (org, org, org, org)).fetchall()
            return [dict(row) for row in rows]

    def totals(self, user: str, org: str, project: str | None = None) -> dict:
        with self.store.transaction() as db:
            role = self.store._require(db, user, org)
            if project is not None:
                self.store._project_access(db, user, org, project)
            # Aggregate in SQLite rather than materializing the installation's entire call history.
            query = """WITH latest_calls AS (
                SELECT i.run_id,i.reserved_microusd,
                  json_extract(e.data_json,'$.prompt_tokens') AS prompt_tokens,
                  json_extract(e.data_json,'$.completion_tokens') AS completion_tokens,
                  json_extract(e.data_json,'$.actual_microusd') AS actual_microusd,
                  json_extract(e.data_json,'$.accounting_state') AS accounting_state
                FROM inference_calls i JOIN inference_call_events e ON e.sequence=(
                    SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id)
                WHERE i.organization_id=?
            ), accounted AS (
                SELECT run_id,reserved_microusd,prompt_tokens,completion_tokens,actual_microusd,accounting_state FROM latest_calls
                UNION ALL
                SELECT id,reserved_microusd,prompt_tokens,completion_tokens,actual_microusd,accounting_state FROM llm_runs r
                WHERE organization_id=? AND NOT EXISTS(SELECT 1 FROM inference_calls i WHERE i.run_id=r.id)
            ), usage AS (
                SELECT p.id AS project_id,CASE WHEN p.deleted_at IS NULL THEN p.name ELSE '[deleted project]' END AS project_name,
                  a.prompt_tokens,a.completion_tokens,a.actual_microusd,a.accounting_state,a.reserved_microusd,
                  CASE WHEN a.prompt_tokens IS NOT NULL AND a.completion_tokens IS NOT NULL
                    THEN (a.prompt_tokens*COALESCE(json_extract(r.impact_profile_json,'$.input_kwh_per_million'),?)
                     +a.completion_tokens*COALESCE(json_extract(r.impact_profile_json,'$.output_kwh_per_million'),?))
                     /1000000.0 END AS energy,
                  CASE WHEN r.impact_profile_json IS NULL AND a.prompt_tokens IS NOT NULL AND a.completion_tokens IS NOT NULL
                    THEN 1 ELSE 0 END AS reference_energy,
                  json_extract(r.impact_profile_json,'$.grid_grams_co2e_per_kwh') AS intensity
                FROM accounted a JOIN llm_runs r ON r.id=a.run_id
                JOIN llm_requests q ON q.organization_id=r.organization_id AND q.id=r.request_id
                JOIN topics t ON t.organization_id=q.organization_id AND t.id=q.topic_id
                JOIN channels c ON c.organization_id=t.organization_id AND c.id=t.channel_id
                JOIN projects p ON p.organization_id=c.organization_id AND p.id=c.project_id
                LEFT JOIN project_memberships pm ON pm.organization_id=p.organization_id AND pm.project_id=p.id AND pm.user_id=?
                WHERE r.organization_id=? AND (p.visibility='organization' OR ? IN ('owner','admin') OR pm.user_id IS NOT NULL)
            ) SELECT project_id,project_name,COUNT(*) AS runs,
              COUNT(prompt_tokens) AS input_known_runs,COUNT(completion_tokens) AS output_known_runs,
              COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,COALESCE(SUM(completion_tokens),0) AS completion_tokens,
              COUNT(actual_microusd) AS cost_known_runs,COALESCE(SUM(actual_microusd),0) AS actual_microusd,
              COALESCE(SUM(CASE WHEN accounting_state IN ('reserved','uncertain') THEN reserved_microusd ELSE 0 END),0)
                AS outstanding_microusd,
              COUNT(energy) AS energy_known_runs,SUM(energy) AS estimated_kwh,SUM(reference_energy) AS reference_energy_runs,
              COUNT(energy*intensity) AS carbon_known_runs,SUM(energy*intensity/1000.0) AS estimated_kg_co2e
              FROM usage """
            arguments = [org, org, REFERENCE_KWH_PER_MILLION, REFERENCE_KWH_PER_MILLION, user, org, role]
            if project is not None:
                query += "WHERE project_id=? "
                arguments.append(project)
            rows = [dict(row) for row in db.execute(query + "GROUP BY project_id,project_name ORDER BY project_name,project_id", arguments)]
            fields = (
                "runs",
                "input_known_runs",
                "output_known_runs",
                "prompt_tokens",
                "completion_tokens",
                "cost_known_runs",
                "actual_microusd",
                "outstanding_microusd",
                "energy_known_runs",
                "reference_energy_runs",
                "carbon_known_runs",
            )
            total = {field: sum(row[field] for row in rows) for field in fields}
            for field in ("estimated_kwh", "estimated_kg_co2e"):
                known = [row[field] for row in rows if row[field] is not None]
                total[field] = sum(known) if known else None
            return {
                "organization_id": org,
                "project_id": project,
                "total": total,
                "projects": rows,
                "scope": "All recorded beta and workflow provider calls, including continuations, compaction, partial/failed calls, "
                "and pending unsubmitted runs; no CAS or human-message energy.",
                "method": "Configured operational kWh per million input/output tokens, or a reference GPU-only scenario "
                "of 1 J per processed token. Reference coefficients are illustrative, not calibrated model measurements. "
                "CO2e requires a configured location grid gCO2e/kWh. "
                "Not provider measurements; excludes embodied hardware, training, networking, and user devices. "
                "Missing values are unknown; totals cover only known values.",
            }
