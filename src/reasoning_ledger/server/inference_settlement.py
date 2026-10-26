"""Settle uncertain model charges from the provider's own generation record.

A cancelled or interrupted call whose usage never arrived keeps its reservation
as uncertain. When the call has an OpenRouter generation ID, the provider's
generation statistics state what was charged. Reading them settles the call
without replaying the request. A call without a generation ID, or whose record
cannot be read on schedule, stays uncertain for administrator reconciliation.
"""

from __future__ import annotations

import asyncio
import json
import time

from .inference_credit import lock as credit_lock
from .openrouter import GENERATION_ID, ProviderError, Quote
from .usage_store import UsageStore

# Seconds after the run stopped at which each lookup is due. The provider
# records a generation some time after it ends; the last lookup is final.
LOOKUP_DELAYS = (30, 120, 600, 3600, 21600)
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens")


class InferenceSettlement:
    def __init__(self, inference):
        self.inference, self.store = inference, inference.store

    def due(self, now: int | None = None, *, limit: int = 16) -> list[dict]:
        """Uncertain calls with a generation ID whose next scheduled lookup is due."""
        now = int(time.time()) if now is None else now
        with self.store.transaction() as db:
            rows = db.execute(
                "SELECT i.id AS call_id,i.organization_id,i.run_id,i.quote_json,e.data_json,r.key_id,r.finished_at "
                "FROM organizations o JOIN llm_runs r ON r.organization_id=o.id AND r.accounting_state='uncertain' "
                "JOIN inference_calls i ON i.run_id=r.id "
                "JOIN inference_call_events e ON e.sequence=(SELECT MAX(sequence) FROM inference_call_events WHERE call_id=i.id) "
                "WHERE r.status NOT IN ('queued','running') AND json_extract(e.data_json,'$.accounting_state')='uncertain' "
                "AND json_extract(e.data_json,'$.provider_generation_id') LIKE 'gen-%' "
                "ORDER BY r.finished_at,i.sequence LIMIT 1000"
            ).fetchall()
            result = []
            for row in rows:
                attempts = max(
                    (
                        event.get("attempt", 0)
                        for event in (
                            json.loads(item[0])
                            for item in db.execute(
                                "SELECT event_json FROM llm_events WHERE organization_id=? AND run_id=? "
                                "AND json_extract(event_json,'$.type')='accounting_lookup'",
                                (row["organization_id"], row["run_id"]),
                            )
                        )
                        if event.get("call_id") == row["call_id"]
                    ),
                    default=0,
                )
                if attempts < len(LOOKUP_DELAYS) and (row["finished_at"] or 0) + LOOKUP_DELAYS[attempts] <= now:
                    generation = json.loads(row["data_json"])["provider_generation_id"]
                    result.append({**dict(row), "attempt": attempts + 1, "generation": generation})
                    if len(result) >= limit:
                        break
            return result

    def _key(self, call: dict) -> str | None:
        """The run's own organization key; a disabled key is never used again."""
        with self.store.transaction() as db:
            key = db.execute(
                "SELECT * FROM provider_keys WHERE organization_id=? AND id=?", (call["organization_id"], call["key_id"])
            ).fetchone()
            if key is None or key["disabled_at"] is not None:
                return None
            return self.store._box.open(
                key["ciphertext"], organization_id=key["organization_id"], provider=key["provider"], key_id=key["id"]
            )

    async def run(self, provider, *, now: int | None = None, limit: int = 16) -> int:
        """Look up each due call outside any transaction; return how many calls settled."""
        if not hasattr(provider, "generation"):
            return 0
        settled = 0
        for call in await asyncio.to_thread(self.due, now, limit=limit):
            record = None
            if not GENERATION_ID.fullmatch(call["generation"]):
                outcome = "unsupported_generation_id"
            else:
                key = await asyncio.to_thread(self._key, call)
                if key is None:
                    outcome = "key_unavailable"
                else:
                    origin = Quote(**json.loads(call["quote_json"])).origin(provider.base_url)
                    try:
                        record = await provider.generation(key, call["generation"], origin=origin)
                    except ProviderError as exc:
                        outcome = exc.code
                    else:
                        outcome = "settled" if record is not None else "not_found"
            settled += await asyncio.to_thread(self.record, call, outcome, record)
        return settled

    def record(self, call: dict, outcome: str, record: dict | None = None) -> bool:
        """Append the lookup outcome; with a provider record, settle the call and roll up its run."""
        org, run, call_id = call["organization_id"], call["run_id"], call["call_id"]
        with self.store.transaction(write=True) as db:
            credit_lock(self.store, db, org)
            last = db.execute(
                "SELECT data_json FROM inference_call_events WHERE call_id=? ORDER BY sequence DESC LIMIT 1", (call_id,)
            ).fetchone()
            data = json.loads(last[0])
            if data.get("accounting_state") != "uncertain":
                return False  # An administrator reconciled it during the lookup.
            attempt = len(LOOKUP_DELAYS) if outcome == "unsupported_generation_id" else call["attempt"]
            lookup = {"type": "accounting_lookup", "call_id": call_id, "attempt": attempt, "outcome": outcome}
            if record is None:
                if attempt >= len(LOOKUP_DELAYS):
                    lookup["final"] = True
                self.inference._event(db, org, run, lookup)
                return False
            now = int(time.time())
            receipt = {
                **data,
                "accounting_state": "settled",
                "actual_microusd": record["cost_microusd"],
                **{field: data[field] if data.get(field) is not None else record[field] for field in TOKEN_FIELDS},
                "settled_from": "provider_generation",
                "provider_cancelled": record["cancelled"],
                "settled_at": now,
            }
            UsageStore._receipt(db, org, call_id, "provider_settlement", receipt)
            cost = UsageStore.settle_run(db, run)
            self.inference._event(db, org, run, {**lookup, "cost_microusd": record["cost_microusd"]})
            if cost is not None:
                self.inference._event(db, org, run, {"type": "accounting", "cost_microusd": cost, "source": "provider_generation"})
            self.store._audit(db, org, None, "inference.cost-settle", call_id)
            return True
