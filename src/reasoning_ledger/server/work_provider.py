"""Server-owned streaming inference, durable intents and retained call outcomes."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, replace

import httpx

from .inference_credit import lock as credit_lock
from .inference_store import InferenceStore
from .openrouter import ProviderError, protocol_diagnostics, safe_diagnostics
from .provider_keys import active_key
from .research_context import encoded
from .research_provider import ResearchCompletion
from .security import digest
from .store import StoreError, _id, _row
from .work_contracts import WorkSpecification
from .work_job_contracts import EffectIntent


class WorkStream(ResearchCompletion):
    """The shared protocol parser with work-owned persistence and cancellation."""

    def __init__(self, owner, credential, body, call, quote):
        self.owner, self.credential, self.body, self.call = owner, credential, body, call
        self.provider, self.quote = owner.provider, quote
        self.stream_visible = True
        self.protocol_stage, self.received_bytes = "stream_framing", 0
        self.partial_usage, self.partial_generation = None, None
        self.next_cancel_check = 0.0

    def _check_cancel(self):
        now = time.monotonic()
        if now < self.next_cancel_check:
            return
        self.next_cancel_check = now + 0.2
        if not self.owner.authorized(self.credential, self.call["job_id"], self.body, self.call["key_id"]):
            raise ProviderError("execution_cancelled")

    def _stream_prefix(self):
        return ""

    def _stream_first_token(self):
        self.owner.event(self.call, {"phase": "first_token"})

    def _stream_response_headers(self):
        self.owner.event(self.call, {"phase": "response_headers"})

    def _stream_content(self, text, generation):
        self.owner.event(self.call, {"phase": "content", "text": text[-400:], "generation_id": generation})
        return True

    def _stream_summary(self, brief):
        self.owner.event(self.call, {"phase": "reasoning_summary", "summary": brief})

    async def _check_cancel_async(self):
        if time.monotonic() >= self.next_cancel_check:
            await asyncio.to_thread(self._check_cancel)

    async def _stream_content_async(self, text, generation):
        return await asyncio.to_thread(self._stream_content, text, generation)

    async def _stream_summary_async(self, brief):
        await asyncio.to_thread(self._stream_summary, brief)


class WorkProvider:
    def __init__(self, jobs, provider):
        self.jobs, self.store, self.provider = jobs, jobs.store, provider

    def _authorize(self, db, credential, job, body):
        row = self.jobs._lease(db, credential, job, body)
        task, root = self.jobs._authorized_task(db, row)
        if root["status"] != "active" or row["cancel_requested"]:
            raise StoreError(409, "work is paused or cancelled")
        if body.payload.model not in WorkSpecification.model_validate_json(task["body_json"]).resources.allowed_models:
            raise StoreError(403, "model is not granted to this task")
        policy = db.execute("SELECT * FROM organizations WHERE id=?", (row["organization_id"],)).fetchone()
        packet = self.jobs._packet(db, row)
        if body.payload.messages[0] != {"role": "system", "content": packet["prompt"]["system_prompt"]} or any(
            message["role"] == "system" for message in body.payload.messages[1:]
        ):
            raise StoreError(403, "completion must retain the assignment's exact pinned system prompt")
        controller = db.execute(
            "SELECT c.root_revision,c.status FROM work_controller_steps s JOIN work_controllers c ON c.id=s.controller_id WHERE s.job_id=?",
            (job,),
        ).fetchone()
        if controller and (controller["root_revision"] != root["head_revision"] or controller["status"] != "running"):
            raise StoreError(409, "controller authority changed; no provider submission was attempted")
        advertised = {item["function"]["name"]: item for item in packet["tools"]}
        names = []
        for item in body.payload.tools:
            function = item.get("function") if isinstance(item, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            if not isinstance(name, str) or advertised.get(name) != item:
                raise StoreError(403, "completion tool schema is not granted to this assignment")
            names.append(name)
        if len(names) != len(set(names)):
            raise StoreError(400, "completion tools must be unique")
        choice = body.payload.tool_choice
        if isinstance(choice, dict) and (
            choice.get("type") != "function" or not isinstance(choice.get("function"), dict) or choice["function"].get("name") not in names
        ):
            raise StoreError(403, "forced completion tool is not granted")
        key = active_key(db, row["organization_id"])
        if key is None:
            raise StoreError(409, "an administrator must enable an organization OpenRouter key")
        return row, root, dict(policy), dict(key)

    def authorized(self, credential, job, body, key):
        try:
            # A streaming reader must not take writer locks just to check a fence.
            with self.store.transaction() as db:
                worker = self.jobs._worker(db, credential)
                row = _row(db.execute("SELECT * FROM work_jobs WHERE id=? AND worker_id=?", (job, worker["id"])).fetchone())
                if (
                    row["status"] != "leased"
                    or row["fence"] != body.fence
                    or row["attempt_id"] != body.attempt_id
                    or row["lease_expires_at"] <= int(time.time())
                    or row["cancel_requested"]
                ):
                    return False
                task, root = self.jobs._authorized_task(db, row)
                controller = db.execute(
                    "SELECT c.root_revision,c.status FROM work_controller_steps s "
                    "JOIN work_controllers c ON c.id=s.controller_id WHERE s.job_id=?",
                    (job,),
                ).fetchone()
                if controller and (controller["root_revision"] != root["head_revision"] or controller["status"] != "running"):
                    return False
                specification = db.execute(
                    "SELECT body_json FROM work_specifications WHERE program_id=? AND revision=?", (root["id"], root["head_revision"])
                ).fetchone()[0]
                limits = WorkSpecification.model_validate_json(specification).resources
                ancestors = db.execute(
                    "SELECT t.body_json FROM work_task_ancestors a JOIN work_tasks t ON t.id=a.ancestor_id WHERE a.task_id=?",
                    (task["id"],),
                ).fetchall()
                now = int(time.time())
                for ancestor in ancestors:
                    envelope = WorkSpecification.model_validate_json(ancestor["body_json"]).resources
                    if (
                        body.payload.model not in envelope.allowed_models
                        or envelope.deadline_at is not None
                        and envelope.deadline_at <= now
                    ):
                        return False
                return (
                    root["status"] == "active"
                    and body.payload.model in limits.allowed_models
                    and (limits.deadline_at is None or limits.deadline_at > now)
                    and bool(
                        db.execute(
                            "SELECT 1 FROM provider_keys WHERE id=? AND organization_id=? AND disabled_at IS NULL",
                            (key, row["organization_id"]),
                        ).fetchone()
                    )
                )
        except StoreError:
            return False

    def event(self, call, event):
        with self.store.transaction(write=True, scope=f"work-program:{call['program_id']}") as db:
            db.execute(
                "INSERT INTO work_stream_events(organization_id,program_id,job_id,fence,call_id,event_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (call["organization_id"], call["program_id"], call["job_id"], call["fence"], call["id"], encoded(event), int(time.time())),
            )

    def progress(self, credential, job, body):
        program = self.jobs._job_scope(credential, job)
        event = encoded({"phase": body.phase, "summary": body.summary})
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row = self.jobs._lease(db, credential, job, body)
            _, root = self.jobs._authorized_task(db, row)
            if row["cancel_requested"] or root["status"] != "active":
                raise StoreError(409, "work is paused or cancelled")
            previous = db.execute(
                "SELECT sequence,event_json FROM work_stream_events WHERE job_id=? AND fence=? AND client_id=?",
                (job, body.fence, body.client_id),
            ).fetchone()
            if previous:
                if previous["event_json"] != event:
                    raise StoreError(409, "progress identity was used for different input")
                return {"sequence": previous["sequence"]}
            db.execute(
                "INSERT INTO work_stream_events(organization_id,program_id,job_id,fence,client_id,event_json,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (row["organization_id"], program, job, body.fence, body.client_id, event, int(time.time())),
            )
            return {
                "sequence": db.execute(
                    "SELECT sequence FROM work_stream_events WHERE job_id=? AND fence=? AND client_id=?",
                    (job, body.fence, body.client_id),
                ).fetchone()[0]
            }

    @staticmethod
    def _view(row):
        return {
            "call_id": row["id"],
            "effect_id": row["effect_id"],
            "state": row["state"],
            "purpose": row["purpose"],
            "response": json.loads(row["response_json"]) if row["response_json"] else None,
            "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
            "error_code": row["error_code"],
            "diagnostics": json.loads(row["diagnostics_json"]) if row["diagnostics_json"] else None,
        }

    def _finish(self, call, reader, response, error):
        usage = reader.partial_usage
        state = "settled" if usage is not None else "rejected" if error is not None and error.no_charge else "unknown"
        actual = usage.cost_microusd if usage is not None else 0 if state == "rejected" else None
        with self.store.transaction(write=True, scope=f"work-program:{call['program_id']}") as db:
            credit_lock(self.store, db, call["organization_id"])
            retained = db.execute("SELECT * FROM work_provider_calls WHERE id=?", (call["id"],)).fetchone()
            if retained["state"] != "running":
                return self._view(retained)
            effect = db.execute("SELECT * FROM work_effects WHERE id=?", (call["effect_id"],)).fetchone()
            if effect["state"] in {"settled", "rejected"}:
                # A separately reconciled effect is authoritative; never overwrite
                # that evidence with a late competing completion.
                state, actual = effect["state"], effect["actual_microusd"]
            else:
                if state != "unknown":
                    self.jobs._account_delta(db, effect, actual, 0)
                receipt = encoded(
                    {
                        "source": "server_provider_receipt",
                        "call_id": call["id"],
                        "generation_id": reader.partial_generation,
                        "usage": asdict(usage) if usage else None,
                        "error_code": error.code if error else None,
                    }
                )
                db.execute(
                    "UPDATE work_effects SET state=?,actual_microusd=?,actual_cpu_seconds=?,receipt_json=?,finished_at=? WHERE id=?",
                    (state, actual, 0 if state != "unknown" else None, receipt, int(time.time()), call["effect_id"]),
                )
            db.execute(
                "UPDATE work_provider_calls SET state=?,response_json=?,usage_json=?,provider_generation_id=?,"
                "error_code=?,diagnostics_json=?,finished_at=? WHERE id=?",
                (
                    "failed" if error else "complete",
                    encoded(response) if response else None,
                    encoded(asdict(usage)) if usage else None,
                    reader.partial_generation,
                    error.code if error else None,
                    encoded(safe_diagnostics(error.diagnostics)) if error and error.diagnostics else None,
                    int(time.time()),
                    call["id"],
                ),
            )
            self.jobs.work._event(
                db,
                call["organization_id"],
                call["program_id"],
                None,
                {
                    "type": "provider_complete",
                    "call_id": call["id"],
                    "accounting_state": state,
                    "error_code": error.code if error else None,
                },
                call["task_id"],
            )
            return self._view(db.execute("SELECT * FROM work_provider_calls WHERE id=?", (call["id"],)).fetchone())

    async def model_info(self, credential, job, body):
        from .work_provider_contracts import WorkCompletion

        packet = await asyncio.to_thread(self.jobs.context, credential, job, body)
        request = WorkCompletion(
            **body.model_dump(exclude={"model"}),
            client_id="model-information",
            payload={"model": body.model, "messages": [{"role": "system", "content": packet["prompt"]["system_prompt"]}]},
        )
        program = self.jobs._job_scope(credential, job)
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            _, _, _, key = self._authorize(db, credential, job, request)
        quotes = await self.provider.quotes([body.model])
        if not await asyncio.to_thread(self.authorized, credential, job, request, key["id"]):
            raise StoreError(409, "model information request lost its assignment authority")
        quote = quotes[0]
        return {
            "model": body.model,
            "context_length": quote.context_length,
            "max_completion_tokens": quote.max_completion_tokens,
            "supports_tools": quote.supports_tools,
        }

    async def complete(self, credential, job, body):
        program = self.jobs._job_scope(credential, job)
        payload = body.payload.model_dump(exclude_none=True)
        request_json = encoded(payload)
        checksum = digest(request_json if body.purpose == "work" else encoded({"purpose": body.purpose, "payload": payload}))
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row, root, policy, key = self._authorize(db, credential, job, body)
            old = db.execute(
                "SELECT c.* FROM work_provider_calls c JOIN work_effects e ON e.id=c.effect_id "
                "WHERE e.job_id=? AND e.fence=? AND e.client_id=?",
                (job, body.fence, body.client_id),
            ).fetchone()
            if old:
                if old["request_sha256"] != checksum or old["purpose"] != body.purpose:
                    raise StoreError(409, "provider call identity was used for different input")
                return self._view(old)
        quotes = await self.provider.quotes([body.payload.model])
        quote = replace(quotes[0], routing=json.loads(policy["routing_policy_json"]))
        if body.payload.tools and not quote.supports_tools:
            raise StoreError(409, "the selected model does not support tools")
        priced = [{"role": "user", "content": request_json}]
        output = InferenceStore.output_capacity(quote, priced, body.payload.max_tokens)
        reserve = quote.reservation(priced, output)
        effect_body = EffectIntent(
            **body.model_dump(include={"attempt_id", "fence", "lease"}),
            client_id=body.client_id,
            kind="inference",
            model=body.payload.model,
            request_sha256=checksum,
            held_microusd=reserve,
        )
        intent = self.jobs.effect_intent(credential, job, effect_body)
        if not intent["submit"]:
            with self.store.transaction() as db:
                old = db.execute("SELECT * FROM work_provider_calls WHERE effect_id=?", (intent["id"],)).fetchone()
                if old:
                    return self._view(old)
            return {
                "call_id": None,
                "effect_id": intent["id"],
                "state": "submission_unknown",
                "response": None,
                "usage": None,
                "error_code": "submission_receipt_missing",
                "diagnostics": None,
            }
        identity = _id()
        # The intent was retained before this transaction. If preparation or the
        # response is lost, a replay never gets a second permission to submit.
        with self.store.transaction(write=True, scope=f"work-program:{program}") as db:
            row, root, current_policy, current_key = self._authorize(db, credential, job, body)
            if current_key["id"] != key["id"] or current_policy["routing_policy_json"] != policy["routing_policy_json"]:
                raise StoreError(409, "provider authorization changed before submission; reconcile retained intent")
            if db.execute("SELECT state FROM work_effects WHERE id=?", (intent["id"],)).fetchone()[0] != "intent":
                raise StoreError(409, "inference intent was already resolved; no provider submission was attempted")
            db.execute(
                "INSERT INTO work_provider_calls(id,effect_id,organization_id,project_id,job_id,fence,worker_id,model,key_id,"
                "request_json,request_sha256,quote_json,state,started_at,purpose) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'running',?,?)",
                (
                    identity,
                    intent["id"],
                    row["organization_id"],
                    root["project_id"],
                    job,
                    body.fence,
                    row["worker_id"],
                    body.payload.model,
                    key["id"],
                    request_json,
                    checksum,
                    encoded(quote.record()),
                    int(time.time()),
                    body.purpose,
                ),
            )
            call = dict(db.execute("SELECT * FROM work_provider_calls WHERE id=?", (identity,)).fetchone()) | {
                "program_id": program,
                "task_id": row["task_id"],
            }
            db.execute("INSERT INTO work_provider_call_sequences(call_id) VALUES(?)", (identity,))
            secret = self.store._box.open(
                key["ciphertext"], organization_id=row["organization_id"], provider="openrouter", key_id=key["id"]
            )
        reader = WorkStream(self, credential, body, call, quote)
        self.event(call, {"phase": "round", "purpose": body.purpose, "model": body.payload.model, "call_id": identity})
        response, error = None, None
        try:
            if any(ord(char) < 32 or ord(char) > 126 for char in secret):
                raise ProviderError("provider_key_invalid", no_charge=True)
            if not self.authorized(credential, job, body, key["id"]):
                raise ProviderError("execution_cancelled", no_charge=True)
            request = dict(payload)
            requested = request.pop("max_tokens", None)
            if requested is not None:
                request["max_completion_tokens"] = requested
            if not request.get("tools"):
                request.pop("tools", None)
                request.pop("tool_choice", None)
            request.update(stream=True, usage={"include": True}, provider=quote.route())
            response = await reader._agent_stream(secret, request, 1800)
            if reader.partial_usage is None:
                raise ProviderError("usage_unavailable")
            usage = reader.partial_usage
            response["usage"] = {
                "cost": usage.cost_microusd / 1_000_000,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
            }
            encoded(response).encode("utf8")
        except ProviderError as exc:
            error = exc
        except (httpx.HTTPError, TimeoutError):
            error = ProviderError("provider_network")
        except (UnicodeError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
            error = ProviderError("provider_protocol", diagnostics=protocol_diagnostics(reader.protocol_stage))
        except asyncio.CancelledError:
            error = ProviderError("execution_cancelled")
        return self._finish(call, reader, response if error is None else None, error)

    def read(self, credential, call_id):
        with self.store.transaction() as db:
            worker = self.jobs._worker(db, credential)
            row = _row(db.execute("SELECT * FROM work_provider_calls WHERE id=? AND worker_id=?", (call_id, worker["id"])).fetchone())
            job = db.execute("SELECT * FROM work_jobs WHERE id=?", (row["job_id"],)).fetchone()
            self.jobs._authorized_task(db, job)
            return self._view(row)

    def events(self, user, org, project, program, job, after=0, limit=100):
        if not 1 <= limit <= 100 or after < 0:
            raise StoreError(400, "invalid work event page")
        with self.store.transaction() as db:
            self.jobs.work._program(db, user, org, project, program)
            if not db.execute("SELECT 1 FROM work_jobs WHERE id=? AND program_id=?", (job, program)).fetchone():
                raise StoreError(404, "job unavailable")
            rows = db.execute(
                "SELECT sequence,fence,event_json,created_at FROM work_stream_events WHERE job_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (job, after, limit + 1),
            ).fetchall()
            return {
                "items": [
                    {
                        "sequence": row["sequence"],
                        "fence": row["fence"],
                        "event": json.loads(row["event_json"]),
                        "created_at": row["created_at"],
                    }
                    for row in rows[:limit]
                ],
                "next": rows[limit - 1]["sequence"] if len(rows) > limit else None,
            }
